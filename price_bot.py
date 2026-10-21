"""
Бот для подготовки и публикации прайса в канале (версия 19).

Как пользоваться:
1. Вставьте боту текст прайса (можно несколькими сообщениями).
2. Бот ждёт 1 минуту после последнего сообщения (или нажмите «Собрать сейчас»).
3. Бот присылает черновик: лишнее убрано, наценка добавлена, модели выделены.
4. Нажмите «Опубликовать». Если в канале уже есть посты с прайсом, бот СПРОСИТ,
   какой из них заменить (⭐ подсказка по названию модели), или создать новый пост,
   или пропустить. Кнопка «Автоподбор» подбирает посты сама по названию модели.
5. Бот ведёт в канале пост-меню с кнопками: названия iPhone (ведут к нужному
   посту), «Заказать», «Ремонт», «О нас». Старые кнопки сохраняются.
6. Кнопки внизу чата: «Новый пост» (текст или одно фото/видео + кнопки-ссылки) и
   «Править пост» (изменить текст или кнопки поста, который уже в канале).
   Везде сначала предпросмотр, в канал уходит только после подтверждения.
   Цвет кнопки: в конце строки - style:green (или blue, red). Если переслать боту
   пост из канала, он прочитает кнопки этого поста и при правке ничего не потеряет.
   Кнопки можно добавить к существующим, заменить, убрать одну или все.

Переменные окружения (Railway -> Variables):
  BOT_TOKEN              токен ОТДЕЛЬНОГО бота для прайса
  OWNER_ID               ваш Telegram ID (число), только вы можете управлять ботом
  CHANNEL_ID             @имя_канала или -100xxxxxxxxxx
  MARKUP                 наценка в рублях, по умолчанию 5000
  MIN_PRICE_FOR_MARKUP   наценка только на цены от этой суммы, по умолчанию 30000
  WAIT_SECONDS           сколько ждать новые части текста, по умолчанию 60
  COMBINE_GENERATIONS    поколения iPhone, которые идут ОДНИМ постом (по умолчанию 12,13,14,15);
  SPLIT_GENERATIONS      поколения iPhone, которые идут ДВУМЯ постами: обычные (16, 16E,
                         16 Plus) и Pro (16 Pro, 16 Pro Max); по умолчанию 16,17
                         остальные поколения (18 и новее): одно поколение = один пост
  MENU_ENABLED           1 = бот сам ведёт пост-меню с кнопками (по умолчанию 0: кнопки вы делаете сами)
  ORDER_URL              куда ведёт кнопка «Заказать» (@имя, t.me/... или https://...)
  REPAIR_URL             куда ведёт кнопка «Ремонт»
  ABOUT_URL              куда ведёт кнопка «О нас» (например, ссылка на пост)
  MENU_TEXT              текст пост-меню (по умолчанию: выбрать модель или раздел)
  STATE_PATH             файл состояния (по умолчанию state.json; с Volume в Railway
                         укажите, например, /data/state.json, тогда ничего не теряется)
  PRICE_MESSAGE_IDS      id постов с прайсом через запятую (бот подскажет сам)
  MENU_MESSAGE_ID        id поста-меню (бот подскажет сам)
"""
import asyncio
import html
import json
import logging
import os
import re

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе токен виден в логах

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
_channel = os.environ["CHANNEL_ID"].strip()
CHANNEL_ID = int(_channel) if _channel.lstrip("-").isdigit() else _channel
MARKUP = int(os.getenv("MARKUP", "5000"))
MIN_PRICE = int(os.getenv("MIN_PRICE_FOR_MARKUP", "30000"))
WAIT_SECONDS = int(os.getenv("WAIT_SECONDS", "60"))
MENU_TEXT = os.getenv("MENU_TEXT", "📱 Выберите модель iPhone или нужный раздел:")
STATE_FILE = os.getenv("STATE_PATH", "state.json")
VERSION = "19.1 (повторы при сбоях сети)"
MENU_ENABLED = os.getenv("MENU_ENABLED", "0") == "1"  # кнопки в канале: по умолчанию выключены
LIMIT = 3800  # лимит Telegram 4096, берём с запасом (теги тоже считаем)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def retry_call(func, *args, tries=5, **kwargs):
    """Повторяет вызов Telegram, если оборвалась сеть (TimedOut, httpx.ReadError)
    или Telegram просит подождать. Использовать ТОЛЬКО для правок постов:
    повтор правки безопасен, а повтор отправки нового поста мог бы дать дубль."""
    for attempt in range(tries):
        try:
            return await func(*args, **kwargs)
        except RetryAfter as e:
            if attempt == tries - 1:
                raise
            wait = e.retry_after
            wait = wait.total_seconds() if hasattr(wait, "total_seconds") else wait
            logging.warning("Telegram просит подождать %s сек.", wait)
            await asyncio.sleep(wait + 1)
        except NetworkError as e:  # TimedOut тоже сюда
            if attempt == tries - 1:
                raise
            logging.warning(
                "Сбой сети (%s: %s), повтор %s из %s",
                type(e).__name__, e, attempt + 2, tries,
            )
            await asyncio.sleep(2 * (attempt + 1))


def norm_url(value: str) -> str:
    """@имя -> https://t.me/имя, t.me/... -> https://t.me/..."""
    v = (value or "").strip()
    if not v:
        return ""
    if v.startswith("@"):
        return "https://t.me/" + v[1:]
    if v.startswith(("t.me/", "www.")):
        return "https://" + v
    return v


# (текст кнопки, переменная окружения, ссылка)
STATIC_BUTTONS = [
    ("🛒 Заказать", "ORDER_URL", norm_url(os.getenv("ORDER_URL", ""))),
    ("🔧 Ремонт", "REPAIR_URL", norm_url(os.getenv("REPAIR_URL", ""))),
    ("ℹ️ О нас", "ABOUT_URL", norm_url(os.getenv("ABOUT_URL", ""))),
]

# "18 Pro 256Gb Black-124.000🇰🇷 🇭🇰 (1 sim+e sim)" -> название, цена, хвост
# Подходит и вариант с пробелами: "18 Pro Max 2Tb Black - 255.000 (🇭🇰, 1 SIM + eSIM)"
# (дефис может быть длинным: – или —)
PRICE_RE = re.compile(r"^(.*?)\s*[-–—]\s*(\d{1,3}(?:\.\d{3})+)(?!\d)(.*)$")

# Что НЕ копируем: зарядки, блоки/адаптеры, чехлы, дополнительная гарантия,
# позиции asis, оптовые строки "От 10шт ...", строки "+3 месяца -3.000".
EXCLUDE_RE = re.compile(
    r"заряд|чехол|адаптер|adapter|блок"
    r"|\bas[\s\-]?is\b"
    r"|(?:платн|доп)\w*\.?\s*гаранти|applecare"
    r"|\+\s*\d+\s*мес"
    r"|^\s*от\s+\d+",
    re.IGNORECASE,
)

# Ссылки и контакты поставщика: https://..., t.me/..., www..., @username
LINK_RE = re.compile(
    r"(?:https?://|www\.|t\.me/|tg://)\S+|(?<!\w)@\w{3,}", re.IGNORECASE
)

# "iPhone 18 Pro 256/512/1TB/2TB" -> "iPhone 18 Pro"
STORAGE_TAIL_RE = re.compile(
    r"\s+\d+\s*(?:gb|tb)?(?:\s*/\s*\d+\s*(?:gb|tb)?)+\s*$", re.IGNORECASE
)

COLLECT_KB = InlineKeyboardMarkup(
    [[InlineKeyboardButton("⚡ Собрать сейчас", callback_data="now")]]
)
DRAFT_KB = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("✅ Опубликовать", callback_data="pub")],
        [
            InlineKeyboardButton("✏️ Изменить", callback_data="edit"),
            InlineKeyboardButton("🗑 Отмена", callback_data="cancel"),
        ],
    ]
)


# ---------- обработка текста ----------

def fmt(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def clean(line: str) -> str:
    """Убирает звёздочки, лишние пробелы (в том числе неразрывные) и отступы."""
    line = line.replace("*", "").replace("\u200b", "")
    line = re.sub(r"[ \t\u00a0]+", " ", line)
    return line.strip()


def parse(text: str, fresh: bool = True):
    """Разбирает текст на блоки (заголовки модели + строки с ценами).

    fresh=True  - свежий прайс поставщика: фильтры, ссылки, наценка.
    fresh=False - текст, который вы поправили сами: только чистка и оформление,
                  ничего не убирается и наценка второй раз не добавляется.
    Возвращает: блоки, сколько цен с наценкой, сколько строк убрано, подозрительные.
    """
    lines = text.split("\n")
    kinds, new_lines = [], []
    changed = 0

    for line in lines:
        line = clean(line)
        if not line:
            kinds.append("blank")
            new_lines.append("")
            continue
        m = PRICE_RE.match(line)

        if not fresh:
            if m:
                kinds.append("phone")
                new_lines.append(f"{m.group(1).rstrip()}-{m.group(2)}{m.group(3)}")
            else:
                kinds.append("text")
                new_lines.append(line)
            continue

        excluded = bool(EXCLUDE_RE.search(line))
        has_link = bool(LINK_RE.search(line))
        if has_link and m and not excluded:
            # в строке с ценой вырезаем только ссылку
            line = clean(LINK_RE.sub("", line))
            m = PRICE_RE.match(line)
        if excluded or (has_link and not m):
            # строки без цены со ссылкой или @контактом убираем целиком
            kinds.append("drop")
            new_lines.append(line)
        elif m:
            price = int(m.group(2).replace(".", ""))
            if price >= MIN_PRICE:
                price += MARKUP
                changed += 1
            kinds.append("phone")
            new_lines.append(f"{m.group(1).rstrip()}-{fmt(price)}{m.group(3)}")
        else:
            kinds.append("text")
            new_lines.append(line)

    if fresh:
        # Идём снизу вверх: строки с ценой оставляем, а строки без цены
        # (заголовки) оставляем только если они стоят прямо над оставленными
        # ценами (не больше двух подряд). Так пропадают описания и хвосты.
        keep = [False] * len(lines)
        dist = 2
        for i in range(len(lines) - 1, -1, -1):
            k = kinds[i]
            if k == "phone":
                keep[i] = True
                dist = 0
            elif k == "drop":
                dist = 2
            elif k == "text" and dist < 2:
                keep[i] = True
                dist += 1
    else:
        keep = [k in ("phone", "text") for k in kinds]

    blocks, heads, phones = [], [], []
    removed, suspicious = 0, []
    for i, line in enumerate(new_lines):
        if kinds[i] == "blank":
            continue
        if not keep[i]:
            removed += 1
            continue
        if kinds[i] == "text":
            if fresh and re.search(r"\d\.\d{3}", line):
                suspicious.append(line)
            if phones:  # начался новый блок
                blocks.append((heads, phones))
                heads, phones = [], []
            heads.append(line)
        else:
            phones.append(line)
    if heads or phones:
        blocks.append((heads, phones))
    return blocks, changed, removed, suspicious


def render_block(heads, phones) -> str:
    """Название модели жирным, одна пустая строка, затем цены без пропусков."""
    parts = []
    if heads:
        parts.append("\n".join(f"<b>{esc(h)}</b>" for h in heads))
    if phones:
        parts.append("\n".join(esc(p) for p in phones))
    return "\n\n".join(parts)


def build_posts(blocks, block_group=None):
    """Склеивает блоки в посты до LIMIT. Режем только по границе блока
    (а слишком длинный блок по строкам, повторяя заголовок).
    Возвращает посты и словарь: номер блока -> номер поста, где он начинается."""
    rendered = []
    for bi, (heads, phones) in enumerate(blocks):
        r = render_block(heads, phones)
        if len(r) <= LIMIT or not phones:
            rendered.append((bi, r))
            continue
        cur = []
        for p in phones:
            if cur and len(render_block(heads, cur + [p])) > LIMIT:
                rendered.append((bi, render_block(heads, cur)))
                cur = [p]
            else:
                cur.append(p)
        if cur:
            rendered.append((bi, render_block(heads, cur)))

    # block_group: номер группы для каждого блока. Блоки разных групп (разных
    # моделей) никогда не попадают в один пост.
    posts, block_post, post_group, cur, cur_group = [], {}, {}, "", None
    for bi, r in rendered:
        g = block_group[bi] if block_group else None
        if cur and (len(cur) + 2 + len(r) > LIMIT or g != cur_group):
            posts.append(cur)
            cur = r
        else:
            cur = f"{cur}\n\n{r}" if cur else r
        cur_group = g
        block_post.setdefault(bi, len(posts))
        post_group.setdefault(len(posts), g)
    if cur:
        posts.append(cur)
    return posts, block_post, post_group


# ---------- названия моделей, меню, подбор постов ----------

# ---------- раскладка по моделям ----------

# Поколения, которые идут одним постом (13, 14, 15 по умолчанию)
COMBINE_GENS = {
    int(x)
    for x in os.getenv("COMBINE_GENERATIONS", "12,13,14,15").split(",")
    if x.strip().isdigit()
}
# Поколения, которые делятся на ДВА поста: обычные (простая, E, Plus, Air) и Pro/Pro Max
SPLIT_GENS = {
    int(x)
    for x in os.getenv("SPLIT_GENERATIONS", "16,17").split(",")
    if x.strip().isdigit()
}
if len(COMBINE_GENS) > 1:
    LEGACY_TITLE = "iPhone " + ", ".join(str(x) for x in sorted(COMBINE_GENS))
elif COMBINE_GENS:
    LEGACY_TITLE = f"iPhone {min(COMBINE_GENS)}"
else:
    LEGACY_TITLE = ""

# "17 Pro Max 2Tb Blue", "Air 256 Blue", "15 Plus 128 Pink", "17E 256", "iPhone 16 Pro"
MODEL_START_RE = re.compile(
    r"^[^\w]*(?:iphone\s*)?(?:(?P<gen>1[0-9]|[89])(?P<e>e)?|(?P<name>air|duo|fold|flip))"
    r"\b\s*(?P<rest>.*)$",
    re.IGNORECASE,
)
NOT_IPHONE_RE = re.compile(r"airpods|ipad|macbook|imac|mac\s*mini|watch|samsung|galaxy|pixel|xiaomi|huawei|honor|dyson", re.IGNORECASE)
VARIANT_RE = re.compile(r"\s*(pro\s*max|pro|plus|mini|air|\+)(?![a-z])", re.IGNORECASE)
VARIANT_ORDER = ["", "mini", "E", "Plus", "Pro", "Pro Max"]


def model_of(text):
    """Какая это модель: {'gen', 'variant', 'title', 'legacy'} или None."""
    m = MODEL_START_RE.match(text)
    if not m:
        return None
    name = m.group("name")
    if name:
        if name.lower() == "air":
            return {"gen": 17, "variant": "Air", "title": "iPhone 17 Air", "legacy": False}
        return {"gen": None, "variant": name.title(), "title": f"iPhone {name.title()}",
                "legacy": False}
    gen = int(m.group("gen"))
    variant = "E" if m.group("e") else ""
    if not variant:
        vm = VARIANT_RE.match(m.group("rest") or "")
        if vm:
            v = re.sub(r"\s+", " ", vm.group(1).lower())
            if v == "air":
                return {"gen": 17, "variant": "Air", "title": "iPhone 17 Air", "legacy": False}
            variant = {"pro max": "Pro Max", "promax": "Pro Max", "pro": "Pro",
                       "plus": "Plus", "+": "Plus", "mini": "mini"}.get(v, v.title())
    if variant == "E":
        title = f"iPhone {gen}E"
    else:
        title = f"iPhone {gen}" + (f" {variant}" if variant else "")
    return {"gen": gen, "variant": variant, "title": title, "legacy": gen in COMBINE_GENS}


def variant_rank(v) -> int:
    return VARIANT_ORDER.index(v) if v in VARIANT_ORDER else 99


def regroup(blocks):
    """Раскладывает строки с ценами по моделям: одна модель = одна группа = один пост
    (все варианты модели вместе). Поколения из COMBINE_GENS идут одной группой,
    внутри неё модели по порядку (13, 14, 15) с жирными подзаголовками. Строки без
    модели (аксессуары и т.п.) остаются в группе «Прочее» со своими заголовками.
    Возвращает: новые блоки, номер группы каждого блока, названия групп, предупреждения."""
    groups, order, other = {}, [], []
    for heads, phones in blocks:
        head_model = None
        for h in heads:
            # общий заголовок вроде "iPhone 17/17E/Air" не годится как модель строки
            if not re.search(r"\d\s*/\s*(\d|e\b|air)", h, re.IGNORECASE):
                head_model = model_of(h) or head_model
        rest = []
        for line in phones:
            if NOT_IPHONE_RE.search(line):  # AirPods, iPad, часы и т.п. не относим к iPhone
                rest.append(line)
                continue
            mdl = model_of(line) or head_model
            if not mdl:
                rest.append(line)
                continue
            if mdl["legacy"]:
                key = "legacy"
            elif mdl["gen"] in SPLIT_GENS:
                is_pro = mdl["variant"] in ("Pro", "Pro Max")
                key = f"split{mdl['gen']}{'pro' if is_pro else 'base'}"
            elif mdl["gen"] is not None:
                key = f"gen{mdl['gen']}"  # остальные поколения (18 и новее) одним постом
            else:
                key = mdl["title"]
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"subs": {}, "legacy": mdl["legacy"]}
                order.append(key)
            sub = g["subs"].setdefault(mdl["title"], {"model": mdl, "lines": []})
            sub["lines"].append(line)
        if rest:
            other.append((heads, rest))

    def order_key(k):
        g = groups[k]
        gens = [x["model"]["gen"] or 0 for x in g["subs"].values()]
        is_pro = any(x["model"]["variant"] in ("Pro", "Pro Max") for x in g["subs"].values())
        # сначала новые поколения, у каждого: обычные, потом Pro; старые (12-15) в конце
        return (1 if g["legacy"] else 0, -max(gens), 1 if is_pro else 0)

    order.sort(key=order_key)
    new_blocks, block_group, group_title, warnings = [], [], {}, []
    gid = 0
    for key in order:
        g = groups[key]
        subs = list(g["subs"].values())
        if g["legacy"]:
            subs.sort(key=lambda x: (x["model"]["gen"], variant_rank(x["model"]["variant"])))
            group_title[gid] = LEGACY_TITLE
            have = {x["model"]["gen"] for x in subs}
            missing = sorted(COMBINE_GENS - have)
            if missing:
                warnings.append(
                    f"⚠️ В посте «{LEGACY_TITLE}» нет поколений: "
                    f"{', '.join(map(str, missing))}. Если замените им старый пост, "
                    "этих моделей в нём не останется."
                )
        else:
            subs.sort(key=lambda x: variant_rank(x["model"]["variant"]))
            if len(subs) == 1:
                group_title[gid] = subs[0]["model"]["title"]
            else:  # "iPhone 16, 16E, 16 Plus"
                names = [x["model"]["title"].replace("iPhone ", "", 1) for x in subs]
                group_title[gid] = "iPhone " + ", ".join(names)
        for sub in subs:
            new_blocks.append(([sub["model"]["title"]], sub["lines"]))
            block_group.append(gid)
        gid += 1
    if other:
        group_title[gid] = "Прочее"
        for b in other:
            new_blocks.append(b)
            block_group.append(gid)
    return new_blocks, block_group, group_title, warnings


def block_label(heads):
    """Название модели из заголовка блока: 'iPhone 18 Pro 256/512/1TB' -> 'iPhone 18 Pro'."""
    if not heads:
        return None
    label = STORAGE_TAIL_RE.sub("", heads[0]).strip()
    return label or None


def post_labels_and_titles(post_group, group_title, n_posts):
    """Название поста = название его группы (модели). Если модель не влезла в один
    пост и разбита на два, у второго в названии добавляется (2)."""
    labels, titles, seen = [], [], {}
    for j in range(n_posts):
        title = group_title.get(post_group.get(j), f"пост {j + 1}")
        seen[title] = seen.get(title, 0) + 1
        labels.append([title])
        titles.append(title if seen[title] == 1 else f"{title} ({seen[title]})")
    return labels, titles


def menu_items(blocks, block_post, block_group, group_title):
    """Кнопки меню: по одной на группу iPhone (текст кнопки, номер поста в черновике)."""
    items, seen = [], set()
    for bi, (_heads, phones) in enumerate(blocks):
        if not phones:
            continue
        gid = block_group[bi]
        label = group_title.get(gid, "")
        if gid in seen or not label.lower().startswith("iphone"):
            continue
        seen.add(gid)
        items.append((label, block_post.get(bi, 0)))
    return items


def norm_label(s: str) -> str:
    return re.sub(r"\W+", "", s.lower())


def suggest_target(labels, state, used):
    """Какой из уже опубликованных постов подходит по названию модели."""
    want = {norm_label(x) for x in labels}
    if not want:
        return None
    for mid in state["ids"]:
        if mid in used:
            continue
        have = {
            norm_label(x) for x in state["posts"].get(str(mid), {}).get("labels", [])
        }
        if want & have:
            return mid
    return None


def existing_title(state, mid) -> str:
    return state["posts"].get(str(mid), {}).get("title") or f"пост #{mid}"


def msg_link(msg_id: int) -> str:
    if isinstance(CHANNEL_ID, str):
        return f"https://t.me/{CHANNEL_ID.lstrip('@')}/{msg_id}"
    return f"https://t.me/c/{str(CHANNEL_ID).replace('-100', '', 1)}/{msg_id}"


def build_menu_kb(items):
    """items: список [название, id поста в канале]."""
    rows, row = [], []
    for label, mid in items:
        row.append(InlineKeyboardButton(label, url=msg_link(mid)))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    for text, _env, url in STATIC_BUTTONS:
        if url:
            rows.append([InlineKeyboardButton(text, url=url)])
    return InlineKeyboardMarkup(rows) if rows else None


# ---------- состояние ----------

def load_state():
    base = {"ids": [], "menu": None, "posts": {}, "menu_items": [],
            "recent": [], "post_buttons": {}}
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        for k in base:
            if k in d:
                base[k] = d[k]
        return base
    except Exception:
        env = os.getenv("PRICE_MESSAGE_IDS", "")
        base["ids"] = [int(x) for x in env.split(",") if x.strip()]
        menu = os.getenv("MENU_MESSAGE_ID", "").strip()
        base["menu"] = int(menu) if menu.isdigit() else None
        return base


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False)


def reset_work(bd):
    for key in ("buf", "draft", "edit_mode", "route"):
        bd.pop(key, None)


# ---------- команды ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await update.message.reply_text(
        f"Версия бота: {VERSION}\n\n"
        "Всё делается кнопками внизу чата:\n"
        "📝 Новый пост: текст или фото, кнопки-ссылки, предпросмотр.\n"
        "✏️ Править пост: изменить текст или кнопки поста, который уже в канале.\n"
        "💰 Обновить прайс: пришлите текст прайса поставщика.\n"
        "🧹 Прайс заново: забыть старые посты прайса.\n"
        "❌ Отмена: отменить то, что делаете сейчас.\n\n"
        "Кнопкам можно задать цвет: в конце строки - style:green (или blue, red).\n\n"
        "В канал ничего не уходит, пока вы не подтвердите кнопкой.",
        reply_markup=MAIN_KB,
    )


async def new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    state = load_state()
    state.update(ids=[], posts={}, menu_items=[])  # пост-меню остаётся
    save_state(state)
    reset_work(context.application.bot_data)
    await update.message.reply_text(
        "Ок, следующий прайс опубликую новыми постами."
    )


# ---------- чтение текста из сообщения ----------

# поля сообщения, где точно нет прайса (чтобы не принять за текст имя или ссылку)
RAW_SKIP = {
    "from", "sender_chat", "chat", "forward_origin", "forward_from",
    "forward_from_chat", "via_bot", "entities", "caption_entities",
    "reply_markup", "link_preview_options", "reply_to_message",
    "file_id", "file_unique_id", "mime_type", "file_name", "thumbnail",
    "type", "username", "language_code",
}
PRICE_HINT_RE = re.compile(r"\d\.\d{3}")


def raw_strings(obj):
    """Все строки внутри «сырого» сообщения (кроме служебных полей)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k not in RAW_SKIP:
                yield from raw_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from raw_strings(v)
    elif isinstance(obj, str):
        yield obj


def rich_to_text(node) -> str:
    """Текст из блоков нового формата Telegram (rich_message): жирный и прочие
    фрагменты склеиваются, а вставки-эмодзи (custom_emoji, в том числе флаги)
    заменяются на настоящее эмодзи из поля alternative_text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, (list, tuple)):
        return "".join(rich_to_text(x) for x in node)
    if isinstance(node, dict):
        kind = node.get("type")
        if kind == "custom_emoji" or ("alternative_text" in node and "text" not in node):
            return node.get("alternative_text") or ""
        if kind in ("line_break", "break", "br", "newline"):
            return "\n"
        if "text" in node:
            return rich_to_text(node["text"])
        if "items" in node:
            return "\n".join(rich_to_text(x) for x in node["items"])
        if "blocks" in node:
            return "\n".join(rich_to_text(x) for x in node["blocks"])
        if "caption" in node:
            return rich_to_text(node["caption"])
    return ""


def extract_text(msg):
    """Текст сообщения: обычный текст, подпись, блоки rich_message (новый формат
    постов с флагами-эмодзи), а если и их нет, то любые строки с ценами из
    «сырых» данных. Возвращает (текст или None, откуда взят)."""
    text = msg.text or msg.caption
    if text:
        return text, "text"
    try:
        raw = msg.to_dict()
    except Exception:
        return None, None
    rich = raw.get("rich_message")
    if isinstance(rich, dict) and rich.get("blocks"):
        joined = "\n".join(rich_to_text(b) for b in rich["blocks"])
        if joined.strip():
            return joined, "rich"
    strings = list(raw_strings(raw))
    if any(PRICE_HINT_RE.search(s) for s in strings):
        # берём строки с ценами и более-менее длинные (названия моделей над ценами)
        found = [s for s in strings if PRICE_HINT_RE.search(s) or len(s) >= 12]
        return "\n".join(found), "raw"
    return None, None


def describe_message(msg):
    """Для диагностики: названия полей сообщения и короткий дамп для логов."""
    try:
        raw = msg.to_dict()
    except Exception:
        return "не удалось прочитать", ""
    skip = {"message_id", "date", "from", "chat", "forward_origin"}
    fields = ", ".join(k for k in raw if k not in skip) or "только служебные данные"
    return fields, json.dumps(raw, ensure_ascii=False, default=str)[:2500]


# ---------- диагностика: как устроено сообщение (команда /raw) ----------

FLAG_RE = re.compile("[\U0001F1E6-\U0001F1FF]")  # буквы-флаги (🇰🇼 и т.д.)
OUTLINE_SKIP = {"from", "chat", "sender_chat", "date", "message_id"}


def leaves(obj, path=""):
    """Все «листья» сообщения: (путь, значение)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not path and k in OUTLINE_SKIP:
                continue
            yield from leaves(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from leaves(v, f"{path}[{i}]")
    else:
        yield path, obj


def short(v, n=70) -> str:
    s = repr(v)
    return s if len(s) <= n else s[: n - 2] + "…'"


def outline_message(raw):
    """Короткое описание сообщения: общий вид и места, где лежат флаги/эмодзи."""
    lines, counts = [], {}
    for path, v in leaves(raw):
        if path.startswith("rich_message"):
            continue  # блоки разобраны отдельно, см. blocks_summary
        base = re.sub(r"\[\d+\]", "[]", path)
        counts[base] = counts.get(base, 0) + 1
        if counts[base] <= 3:  # по три примера на каждый вид поля
            lines.append(f"{path} = {short(v)}")
    for base, c in counts.items():
        if c > 3:
            lines.append(f"... {base}: всего {c}")
    marks = []
    for path, v in leaves(raw):
        if "emoji" in path.lower() or (
            isinstance(v, str) and (FLAG_RE.search(v) or "📱" in v)
        ):
            marks.append(f"{path} = {short(v, 90)}")
    return lines[:70], marks[:12]


def blocks_summary(raw):
    """Какие бывают блоки в rich_message и как выглядят не-текстовые (картинки и т.п.)."""
    rich = raw.get("rich_message")
    if not isinstance(rich, dict):
        return None
    blocks = rich.get("blocks") or []
    counts, others = {}, []
    for i, b in enumerate(blocks):
        kind = b.get("type") if isinstance(b, dict) else type(b).__name__
        counts[kind] = counts.get(kind, 0) + 1
        if kind != "paragraph" and len(others) < 4:
            others.append((i, b))
    lines = ["Типы блоков: " + ", ".join(f"{k} x{c}" for k, c in counts.items())]
    for k, v in rich.items():  # другие поля rich_message, кроме самих блоков
        if k != "blocks":
            for path, val in leaves(v, f"rich_message.{k}"):
                lines.append(f"{path} = {short(val)}")
    for i, b in others:
        n = 0
        for path, val in leaves(b, f"blocks[{i}]"):
            lines.append(f"{path} = {short(val)}")
            n += 1
            if n >= 12:
                lines.append("...")
                break
    return lines[:40]


async def send_outline(msg):
    try:
        raw = msg.to_dict()
    except Exception as e:
        await msg.reply_text(f"Не удалось прочитать сообщение: {e}")
        return
    summary = blocks_summary(raw)
    if summary:
        await msg.reply_text(
            "Блоки поста:\n<pre>" + esc("\n".join(summary)) + "</pre>",
            parse_mode=ParseMode.HTML,
        )
    lines, marks = outline_message(raw)
    text = "Поля сообщения:\n" + "\n".join(lines)
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3300:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for c in chunks[:2]:
        await msg.reply_text(f"<pre>{esc(c)}</pre>", parse_mode=ParseMode.HTML)
    where = "\n".join(marks) if marks else "флаги и эмодзи в полях не найдены"
    await msg.reply_text(f"Где флаги и эмодзи:\n<pre>{esc(where)}</pre>",
                         parse_mode=ParseMode.HTML)
    logging.info("RAW сообщение: %s", json.dumps(raw, ensure_ascii=False, default=str)[:6000])


async def raw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    context.application.bot_data["raw_mode"] = True
    await update.message.reply_text(
        "Режим диагностики. Перешлите один пост поставщика: я покажу, как он "
        "устроен внутри. В канал и в черновик при этом ничего не уходит."
    )


# ---------- приём текста ----------

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or update.effective_user.id != OWNER_ID:
        return
    if await on_menu_text(update, context):  # нажата кнопка с клавиатуры внизу
        return
    if context.application.bot_data.pop("raw_mode", False):
        await send_outline(msg)
        return
    if context.application.bot_data.get("post"):  # идёт создание поста (/post)
        await on_post_input(update, context)
        return
    text, source = extract_text(msg)
    logging.info(
        "Сообщение: источник текста=%s, символов=%s", source, len(text or "")
    )
    if not text:
        fields, dump = describe_message(msg)
        logging.warning("Сообщение без текста, поля: %s | %s", fields, dump)
        await msg.reply_text(
            "Бот не видит текст в этом сообщении.\n"
            f"Из чего оно состоит: {fields}\n\n"
            "Пришлите этот список мне, я подстрою бота. А пока скопируйте текст "
            "поста (нажмите на текст, затем «Копировать») и вставьте сюда."
        )
        return
    if source == "raw":
        await msg.reply_text("Текст нашёл в служебных полях сообщения, беру его.")
    bd = context.application.bot_data
    buf = bd.setdefault("buf", [])
    first = not buf
    buf.append(text)
    if first:
        await msg.reply_text(
            f"Принял. Жду ещё части {WAIT_SECONDS} сек. после последнего сообщения.",
            reply_markup=COLLECT_KB,
        )
    for job in context.job_queue.get_jobs_by_name("flush"):
        job.schedule_removal()
    context.job_queue.run_once(flush, WAIT_SECONDS, chat_id=msg.chat_id, name="flush")


async def flush(context: ContextTypes.DEFAULT_TYPE):
    await make_draft(context.bot, context.application.bot_data, context.job.chat_id)


async def make_draft(bot, bd, chat_id):
    buf = bd.pop("buf", [])
    edit_mode = bd.pop("edit_mode", False)
    if not buf:
        return
    blocks, changed, removed, suspicious = parse("\n".join(buf), fresh=not edit_mode)
    if not any(phones for _, phones in blocks):
        await bot.send_message(
            chat_id,
            "Цены вида «название-124.000» не найдены. Проверьте, что вставлен прайс.",
        )
        return
    blocks, block_group, group_title, warnings = regroup(blocks)
    posts, block_post, post_group = build_posts(blocks, block_group)
    items = menu_items(blocks, block_post, block_group, group_title)
    labels, titles = post_labels_and_titles(post_group, group_title, len(posts))
    bd["draft"] = {
        "posts": posts,
        "items": items,
        "post_labels": labels,
        "post_titles": titles,
    }
    bd.pop("route", None)

    if edit_mode:
        head = f"Черновик по вашему тексту: постов {len(posts)}."
    else:
        head = (
            f"Черновик: постов {len(posts)}, цен с наценкой {changed}, "
            f"убрано строк {removed}."
        )
    head += "\n\nПосты: " + "; ".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
    if MENU_ENABLED:
        names = [label for label, _ in items] + [t for t, _e, u in STATIC_BUTTONS if u]
        head += "\n\nКнопки в канале: " + (", ".join(names) if names else "нет")
    if warnings:
        head += "\n\n" + "\n".join(warnings)
    if suspicious:
        head += "\n\nПохоже на цену, но не распознано (проверьте):\n"
        head += "\n".join(suspicious[:5])
    await bot.send_message(chat_id, head)
    for post in posts:
        await bot.send_message(chat_id, post, parse_mode=ParseMode.HTML)
    await bot.send_message(chat_id, "Публикуем в канал?", reply_markup=DRAFT_KB)


# ---------- выбор, какой пост заменить ----------

async def ask_route(bot, bd, chat_id):
    draft, r = bd["draft"], bd["route"]
    j, n = r["j"], len(draft["posts"])
    state = load_state()
    used = {v for v in r["map"].values() if isinstance(v, int)}
    sug = suggest_target(draft["post_labels"][j], state, used)

    lines = [
        f"Пост {j + 1} из {n}: <b>{esc(draft['post_titles'][j])}</b>",
        "Какой пост в канале заменить?",
        "",
        "Сейчас в канале:",
    ]
    rows = []
    for k, mid in enumerate(state["ids"]):
        title = existing_title(state, mid)
        taken = mid in used
        lines.append(
            f'{k + 1}. <a href="{msg_link(mid)}">{esc(title)}</a>'
            + (" (уже выбран выше)" if taken else "")
        )
        if taken:
            continue  # один пост канала нельзя заменить дважды
        star = "⭐ " if mid == sug else ""
        rows.append(
            [
                InlineKeyboardButton(
                    f"{star}Заменить {k + 1}. {title[:30]}", callback_data=f"r:{mid}"
                )
            ]
        )
    if sug:
        lines.append("\n⭐ бот считает, что подходит по названию модели")
    if any(not state["posts"].get(str(m), {}).get("title") for m in state["ids"]):
        lines.append(
            "\nНе помните, какой пост что содержит? Нажмите «Показать посты»: "
            "бот пришлёт их копии сюда по номерам."
        )
    rows.append([InlineKeyboardButton("👁 Показать посты", callback_data="r:show")])
    rows.append(
        [
            InlineKeyboardButton("➕ Новый пост", callback_data="r:new"),
            InlineKeyboardButton("⏭ Пропустить", callback_data="r:skip"),
        ]
    )
    last = [InlineKeyboardButton("🗑 Отмена", callback_data="r:cancel")]
    if j == 0:
        last.insert(0, InlineKeyboardButton("⚡ Автоподбор", callback_data="r:auto"))
    rows.append(last)
    await bot.send_message(
        chat_id,
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
        link_preview_options=NO_PREVIEW,
    )


async def on_route(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer()
        return
    bd = context.application.bot_data
    draft, r = bd.get("draft"), bd.get("route")
    chat_id = q.message.chat_id
    choice = q.data[2:]
    if not draft or not r:
        await q.answer()
        await q.edit_message_text("Выбор потерялся. Пришлите прайс заново.")
        return
    n = len(draft["posts"])
    used = {v for v in r["map"].values() if isinstance(v, int)}
    if choice.isdigit() and int(choice) in used:
        await q.answer("Этот пост уже выбран для другого поста", show_alert=True)
        return
    await q.answer()

    if choice == "show":
        # присылаем копии постов канала, чтобы было видно, что в каком
        await q.edit_message_text("Показываю посты из канала…")
        state = load_state()
        for k, mid in enumerate(state["ids"]):
            try:
                await context.bot.send_message(chat_id, f"▼ Пост {k + 1}")
                await context.bot.copy_message(
                    chat_id=chat_id, from_chat_id=CHANNEL_ID, message_id=mid
                )
            except Exception as e:  # например, пост удалён или нет доступа
                logging.warning("Не удалось показать пост %s: %s", mid, e)
                await context.bot.send_message(
                    chat_id,
                    f"Пост {k + 1} показать не удалось, откройте по ссылке: "
                    f"{msg_link(mid)}",
                    link_preview_options=NO_PREVIEW,
                )
        await ask_route(context.bot, bd, chat_id)
        return

    if choice == "cancel":
        bd.pop("route", None)
        await q.edit_message_text(
            "Публикация отменена, в канал ничего не ушло. Черновик остался."
        )
        await context.bot.send_message(
            chat_id, "Публикуем в канал?", reply_markup=DRAFT_KB
        )
        return

    if choice == "auto":
        state = load_state()
        used_auto, mapping = set(), {}
        for j in range(n):
            mid = suggest_target(draft["post_labels"][j], state, used_auto)
            if mid:
                used_auto.add(mid)
                mapping[j] = mid
            else:
                mapping[j] = "new"
        r["map"] = mapping
        r["j"] = n
        await q.edit_message_text("Автоподбор по названиям моделей.")
    else:
        j = r["j"]
        r["map"][j] = int(choice) if choice.isdigit() else choice
        r["j"] += 1
        what = {"new": "новый пост", "skip": "пропускаю"}.get(
            choice, f"заменяю пост #{choice}"
        )
        await q.edit_message_text(f"Пост {j + 1}: {what}.")

    if r["j"] < n:
        await ask_route(context.bot, bd, chat_id)
    else:
        await do_publish(context.bot, bd, chat_id, r["map"])


# ---------- кнопки черновика ----------

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer()
        return
    await q.answer()
    bd = context.application.bot_data
    chat_id = q.message.chat_id
    action = q.data

    if action == "now":
        for job in context.job_queue.get_jobs_by_name("flush"):
            job.schedule_removal()
        await q.edit_message_text("Собираю…")
        await make_draft(context.bot, bd, chat_id)

    elif action == "cancel":
        reset_work(bd)
        await q.edit_message_text("Черновик удалён. В канал ничего не ушло.")

    elif action == "edit":
        bd["edit_mode"] = True
        bd.pop("buf", None)
        await q.edit_message_text(
            "Пришлите исправленный текст целиком (можно скопировать черновик "
            "выше и поправить). Наценка второй раз не добавится."
        )

    elif action == "pub":
        draft = bd.get("draft")
        if not draft:
            await q.edit_message_text(
                "Черновика нет (бот перезапускался). Пришлите прайс заново."
            )
            return
        state = load_state()
        if not state["ids"]:
            # в канале ещё нет постов с прайсом: всё публикуем новыми
            await q.edit_message_text("Публикую…")
            mapping = {j: "new" for j in range(len(draft["posts"]))}
            await do_publish(context.bot, bd, chat_id, mapping)
        else:
            bd["route"] = {"j": 0, "map": {}}
            await q.edit_message_text("Выберите, какие посты в канале заменить.")
            await ask_route(context.bot, bd, chat_id)


async def do_publish(bot, bd, chat_id, mapping):
    draft = bd.pop("draft", None)
    bd.pop("route", None)
    if not draft:
        return
    try:
        res = await publish_routed(bot, draft, mapping)
    except Exception as e:
        # Сеть не выдержала даже после повторов. Черновик возвращаем, чтобы не
        # пришлось присылать прайс заново: нажмите «Опубликовать» ещё раз.
        # Уже заменённые посты при повторе дадут «not modified» и считаются готовыми.
        logging.exception("Публикация прервана")
        bd["draft"] = draft
        await bot.send_message(
            chat_id,
            f"⚠️ Публикация прервалась ({type(e).__name__}). Часть постов в канале "
            "могла уже обновиться. Черновик сохранён: нажмите «Опубликовать» ещё раз "
            "и выберите те же посты, готовые бот пропустит сам.",
            reply_markup=DRAFT_KB,
        )
        return

    lines = []
    for j, (mid, kind) in sorted(res["actual"].items()):
        title = draft["post_titles"][j]
        word = {"edit": "заменён", "new": "новый"}[kind]
        lines.append(f"Пост {j + 1} ({title}): {word} #{mid}")
    for j in range(len(draft["posts"])):
        if j not in res["actual"]:
            lines.append(f"Пост {j + 1} ({draft['post_titles'][j]}): пропущен")
    reply = "✅ Готово.\n\n" + "\n".join(lines)
    if MENU_ENABLED:
        reply += f"\n\nКнопок моделей в меню: {res['buttons']}."

    env_lines = []
    if res["ids"] != res["old_ids"]:
        env_lines.append(f"PRICE_MESSAGE_IDS={','.join(map(str, res['ids']))}")
    if MENU_ENABLED and res["menu_id"] != res["old_menu"]:
        env_lines.append(f"MENU_MESSAGE_ID={res['menu_id']}")
    if env_lines:
        reply += (
            "\n\nДобавьте в Railway переменные, чтобы id не потерялись "
            "после перезапуска:\n" + "\n".join(env_lines)
        )
    missing = [(t, e) for t, e, u in STATIC_BUTTONS if not u] if MENU_ENABLED else []
    if missing:
        reply += "\n\nНет ссылок для кнопок, добавьте переменные в Railway:\n"
        reply += "\n".join(f"{e} ({t})" for t, e in missing)
    await bot.send_message(chat_id, reply)


async def publish_routed(bot, draft, mapping):
    """Публикует посты черновика: каждый либо заменяет выбранный пост канала,
    либо уходит новым постом, либо пропускается. Лишнее ничего не удаляется."""
    state = load_state()
    ids, posts_meta = state["ids"], state["posts"]
    old_ids, old_menu = list(ids), state["menu"]
    menu_id = state["menu"]

    if MENU_ENABLED and menu_id is None:
        # меню создаём первым, чтобы оно было выше прайса
        sent = await bot.send_message(CHANNEL_ID, MENU_TEXT)
        menu_id = sent.message_id

    actual = {}
    for j, text in enumerate(draft["posts"]):
        target = mapping.get(j, "new")
        if target == "skip":
            continue
        meta = {"title": draft["post_titles"][j], "labels": draft["post_labels"][j]}
        if actual:
            await asyncio.sleep(1.5)  # пауза между постами, чтобы Telegram не тормозил
        if target != "new":
            mid = int(target)
            try:
                await retry_call(
                    bot.edit_message_text,
                    text,
                    chat_id=CHANNEL_ID,
                    message_id=mid,
                    parse_mode=ParseMode.HTML,
                )
                actual[j] = (mid, "edit")
                posts_meta[str(mid)] = meta
                state.update(ids=ids, posts=posts_meta)
                save_state(state)  # сохраняем после каждого поста
                continue
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    actual[j] = (mid, "edit")
                    posts_meta[str(mid)] = meta
                    continue
                logging.warning("Не удалось отредактировать %s: %s", mid, e)
                if mid in ids:  # такого поста в канале уже нет
                    ids.remove(mid)
                posts_meta.pop(str(mid), None)
        sent = await bot.send_message(CHANNEL_ID, text, parse_mode=ParseMode.HTML)
        ids.append(sent.message_id)
        posts_meta[str(sent.message_id)] = meta
        actual[j] = (sent.message_id, "new")
        state.update(ids=ids, posts=posts_meta)
        save_state(state)

    # меню: добавляем/обновляем кнопки для опубликованных моделей, старые остаются
    menu_list = [
        [label, mid] for label, mid in state["menu_items"] if mid in ids
    ]
    # в заменённом посте старой модели больше нет: её кнопка не должна вести туда
    for j, (mid, _kind) in actual.items():
        keep = {label.lower() for label, jj in draft["items"] if jj == j}
        menu_list = [
            e for e in menu_list if not (e[1] == mid and e[0].lower() not in keep)
        ]
    for label, j in draft["items"]:
        if j not in actual:
            continue
        mid = actual[j][0]
        for entry in menu_list:
            if entry[0].lower() == label.lower():
                entry[1] = mid
                break
        else:
            menu_list.append([label, mid])
    if MENU_ENABLED:
        kb = build_menu_kb(menu_list)
        try:
            await retry_call(
                bot.edit_message_text,
                MENU_TEXT, chat_id=CHANNEL_ID, message_id=menu_id, reply_markup=kb,
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                logging.warning("Меню не обновилось, отправляю новое: %s", e)
                sent = await bot.send_message(CHANNEL_ID, MENU_TEXT, reply_markup=kb)
                menu_id = sent.message_id

    state.update(ids=ids, menu=menu_id, posts=posts_meta, menu_items=menu_list)
    save_state(state)
    return {
        "actual": actual,
        "ids": ids,
        "old_ids": old_ids,
        "menu_id": menu_id,
        "old_menu": old_menu,
        "buttons": len(menu_list),
    }


# ---------- посты с кнопками: создание и правка (кнопки внизу чата) ----------

CAPTION_LIMIT = 1024    # подпись к фото или видео не длиннее 1024 символов
POST_TEXT_LIMIT = 4096  # обычный текстовый пост не длиннее 4096 символов

# Одна кнопка: «Название - ссылка», «Название - @имя», «Название - t.me/...»,
# «Название - сайт.ру/страница». Тире между названием и ссылкой можно не ставить.
BUTTON_RE = re.compile(
    r"^(?P<name>.+?)\s*[-–—:]?\s*"
    r"(?P<url>(?:https?://|tg://|t\.me/|www\.|@\w"
    r"|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?:/|\b))\S*)$",
    re.IGNORECASE,
)
# ссылка на пост канала: t.me/имя_канала/123 или t.me/c/1234567890/123
LINK_POST_RE = re.compile(
    r"(?:https?://)?t\.me/(?:c/(?P<cid>\d+)|(?P<user>[A-Za-z0-9_]+))/(?P<mid>\d+)",
    re.IGNORECASE,
)

# Постоянная клавиатура внизу чата: команды вводить не нужно.
BTN_NEW = "📝 Новый пост"
BTN_EDIT = "✏️ Править пост"
BTN_PRICE = "💰 Обновить прайс"
BTN_RESET = "🧹 Прайс заново"
BTN_CANCEL = "❌ Отмена"
BTN_HELP = "ℹ️ Помощь"
MAIN_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_NEW), KeyboardButton(BTN_EDIT)],
        [KeyboardButton(BTN_PRICE), KeyboardButton(BTN_RESET)],
        [KeyboardButton(BTN_CANCEL), KeyboardButton(BTN_HELP)],
    ],
    resize_keyboard=True,
)

POST_CANCEL_KB = InlineKeyboardMarkup(
    [[InlineKeyboardButton("🗑 Отмена", callback_data="post:cancel")]]
)
POST_BUTTONS_KB = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("⏭ Без кнопок", callback_data="post:nobtn")],
        [InlineKeyboardButton("🗑 Отмена", callback_data="post:cancel")],
    ]
)
EDIT_MENU_KB = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("✏️ Изменить текст", callback_data="post:etxt")],
        [InlineKeyboardButton("➕ Добавить кнопки к существующим", callback_data="post:eadd")],
        [InlineKeyboardButton("🔘 Заменить все кнопки", callback_data="post:ebtn")],
        [InlineKeyboardButton("➖ Убрать одну кнопку", callback_data="post:edel")],
        [InlineKeyboardButton("🚫 Убрать все кнопки", callback_data="post:erm")],
        [InlineKeyboardButton("↩️ Выбрать другой пост", callback_data="post:repick")],
        [InlineKeyboardButton("🗑 Отмена", callback_data="post:cancel")],
    ]
)

POST_START_TEXT = (
    "✍️ Новый пост (это обычный пост канала, не прайс).\n\n"
    "Пришлите текст поста. Если нужно фото или видео, пришлите ОДНО и напишите "
    "текст в подписи. Жирный, курсив, ссылки и эмодзи сохранятся.\n\n"
    "В канал ничего не уйдёт, пока вы не нажмёте «Опубликовать»."
)
POST_BUTTONS_TEXT = (
    "🔘 Теперь кнопки под постом.\n\n"
    "Одна строка = одна кнопка:\n"
    "название - ссылка\n\n"
    "Пример:\n"
    "Заказать - https://t.me/RMservice99\n"
    "Отзывы - https://www.avito.ru/brands/rmservice\n\n"
    "Две кнопки в одном ряду: поставьте между ними знак |\n"
    "Заказать - t.me/RMservice99 | Отзывы - avito.ru/brands/rmservice\n\n"
    "Цвет кнопки (необязательно): в конце строки - style:green, style:blue или style:red\n"
    "Заказать - t.me/RMservice99 - style:green\n\n"
    "Вместо ссылки можно написать @имя. Кнопки не нужны: нажмите «Без кнопок»."
)
PICK_TEXT = (
    "✏️ Какой пост править?\n\n"
    "Выберите из списка ниже, или перешлите сюда пост из канала, или пришлите "
    "ссылку на него (в канале нажмите на пост и «Копировать ссылку»).\n\n"
    "Чтобы бот мог править посты, он должен быть админом канала с правом "
    "«Изменение сообщений других»."
)


ADD_BUTTONS_TEXT = (
    "➕ Пришлите новые кнопки. Они встанут в конец, под старыми. Старые кнопки "
    "не изменятся.\n\n"
    "Одна строка = одна кнопка: название - ссылка\n"
    "Пример: Отзывы - avito.ru/brands/rmservice - style:blue\n\n"
    "Цвет необязателен (style:green, style:blue, style:red). Знак | ставит кнопки "
    "в один ряд. Ничего не добавлять: нажмите «Не добавлять»."
)
ADD_KB = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("⏭ Не добавлять", callback_data="post:nobtn")],
        [InlineKeyboardButton("🗑 Отмена", callback_data="post:cancel")],
    ]
)
DELETE_TEXT = (
    "➖ Нажмите на кнопку, которую нужно убрать. Остальные не изменятся. "
    "Когда закончите, нажмите «Готово»."
)

# цвета кнопок (Bot API 9.4): primary - синяя, success - зелёная, danger - красная
STYLE_WORDS = {
    "green": "success", "success": "success", "зелёный": "success",
    "зеленый": "success", "зелёная": "success", "зеленая": "success",
    "blue": "primary", "primary": "primary", "синий": "primary", "синяя": "primary",
    "red": "danger", "danger": "danger", "красный": "danger", "красная": "danger",
}
STYLE_ICON = {"success": "🟢", "primary": "🔵", "danger": "🔴"}
STYLE_RE = re.compile(
    r"^(?P<rest>.*?)\s*[-–—]?\s*style\s*[:=]\s*(?P<word>\S+?)\s*$", re.IGNORECASE
)


def buttons_prompt(post):
    # что показать, когда ждём кнопки: добавление к старым или замена всех
    if post.get("append"):
        return ADD_BUTTONS_TEXT, ADD_KB
    return POST_BUTTONS_TEXT, POST_BUTTONS_KB


def buttons_summary(rows):
    if not rows:
        return "Кнопок нет."
    lines = []
    for row in rows:
        parts = []
        for b in row:
            t, _u, s = unpack(b)
            parts.append(f"{STYLE_ICON.get(s, '▫️')} {t}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def remove_button(rows, n):
    # убирает кнопку с порядковым номером n (по всем рядам), пустые ряды выбрасывает
    out, i = [], 0
    for row in rows:
        keep = []
        for b in row:
            if i != n:
                keep.append(b)
            i += 1
        if keep:
            out.append(keep)
    return out


def delete_kb(rows):
    kb, i = [], 0
    for row in rows:
        for b in row:
            kb.append([InlineKeyboardButton(f"❌ {unpack(b)[0][:30]}", callback_data=f"post:rm:{i}")])
            i += 1
    kb.append([InlineKeyboardButton("✅ Готово", callback_data="post:rmdone")])
    kb.append([InlineKeyboardButton("🗑 Отмена", callback_data="post:cancel")])
    return InlineKeyboardMarkup(kb)


def buttons_from_markup(markup):
    # Кнопки-ссылки из пересланного поста: [[(название, ссылка, цвет), ...], ...].
    # Кнопки не со ссылкой (реакции, счётчики) бот перенести не может, их пропускаем.
    rows, skipped = [], 0
    for row in getattr(markup, "inline_keyboard", None) or []:
        out = []
        for b in row:
            url = getattr(b, "url", None)
            if not url:
                skipped += 1
                continue
            style = getattr(b, "style", None) or (getattr(b, "api_kwargs", None) or {}).get("style")
            out.append((b.text, url, style if style in STYLE_ICON else None))
        if out:
            rows.append(out)
    return rows, skipped


def button_url(u: str) -> str:
    u = u.strip()
    if u.startswith("@"):
        return "https://t.me/" + u[1:]
    if re.match(r"(?i)^(https?://|tg://)", u):
        return u
    return "https://" + u


def parse_buttons(text: str):
    # Возвращает ряды кнопок [[(название, ссылка, цвет), ...], ...] и список непонятых строк.
    rows, bad = [], []
    for line in text.split("\n"):
        line = clean(line)
        if not line:
            continue
        row, ok = [], True
        for part in line.split("|"):
            part, style = part.strip(), None
            sm = STYLE_RE.match(part)
            if sm:
                style = STYLE_WORDS.get(sm.group("word").lower())
                if not style:
                    ok = False
                    break
                part = sm.group("rest").strip()
            m = BUTTON_RE.match(part)
            if not m:
                ok = False
                break
            row.append((m.group("name").strip(), button_url(m.group("url")), style))
        if ok and row:
            rows.append(row)
        else:
            bad.append(line)
    return rows, bad


def unpack(b):
    # кнопка хранится как (название, ссылка) или (название, ссылка, цвет)
    return b[0], b[1], (b[2] if len(b) > 2 else None)


def make_button(t, u, s=None):
    if not s:
        return InlineKeyboardButton(t, url=u)
    try:
        return InlineKeyboardButton(t, url=u, style=s)  # python-telegram-bot 22.7+
    except TypeError:  # старая версия библиотеки: передаём поле напрямую
        return InlineKeyboardButton(t, url=u, api_kwargs={"style": s})


def post_kb(rows):
    if not rows:
        return None
    return InlineKeyboardMarkup(
        [[make_button(*unpack(b)) for b in row] for row in rows]
    )


def post_title(plain, media):
    first = (plain or "").strip().split("\n")[0].strip()
    if first:
        return first[:28]
    return "🖼 фото" if media and media[0] == "photo" else "🎬 видео"


# ----- что бот помнит о постах (state.json) -----

def remember_post(mid, title=None, rows=None):
    # Запоминаем пост и его кнопки (с цветами): при правке текста бот ставит их обратно.
    try:
        state = load_state()
        old = next((r for r in state["recent"] if r["id"] == mid), None)
        title = title or (old["title"] if old else f"пост {mid}")
        state["recent"] = [r for r in state["recent"] if r["id"] != mid]
        state["recent"].insert(0, {"id": mid, "title": title})
        state["recent"] = state["recent"][:10]
        if rows:
            state["post_buttons"][str(mid)] = [[list(unpack(b)) for b in row] for row in rows]
        else:
            state["post_buttons"].pop(str(mid), None)
        save_state(state)
    except Exception as e:
        logging.warning("Не удалось запомнить пост %s: %s", mid, e)


def recent_posts(state):
    seen, out = set(), []
    for r in state["recent"]:
        if r["id"] not in seen:
            seen.add(r["id"])
            out.append((r["id"], r["title"]))
    for mid in reversed(state["ids"]):  # посты прайса
        if mid not in seen:
            seen.add(mid)
            out.append((mid, existing_title(state, mid)))
    return out[:10]


def pick_kb(state):
    rows = [
        [InlineKeyboardButton(f"#{mid} {title[:30]}", callback_data=f"post:pick:{mid}")]
        for mid, title in recent_posts(state)
    ]
    rows.append([InlineKeyboardButton("🗑 Отмена", callback_data="post:cancel")])
    return InlineKeyboardMarkup(rows)


# ----- как понять, какой пост править -----

def same_channel(chat) -> bool:
    if isinstance(CHANNEL_ID, int):
        return getattr(chat, "id", None) == CHANNEL_ID
    uname = getattr(chat, "username", None)
    if not uname:
        return True
    return uname.lower() == CHANNEL_ID.lstrip("@").lower()


def target_from_forward(msg):
    # Номер поста из пересланного поста канала: число, "other" (чужой канал) или None.
    fo = getattr(msg, "forward_origin", None)
    mid = getattr(fo, "message_id", None) if fo else None
    chat = getattr(fo, "chat", None) if fo else None
    if not mid:  # старые версии библиотеки
        mid = getattr(msg, "forward_from_message_id", None)
        chat = getattr(msg, "forward_from_chat", None)
    if not mid or chat is None:
        return None
    return int(mid) if same_channel(chat) else "other"


def target_from_text(text):
    m = LINK_POST_RE.search(text or "")
    if not m:
        return None
    mid = int(m.group("mid"))
    if isinstance(CHANNEL_ID, str):
        if m.group("user") and m.group("user").lower() != CHANNEL_ID.lstrip("@").lower():
            return "other"
    elif m.group("cid") and m.group("cid") != str(CHANNEL_ID).replace("-100", "", 1):
        return "other"
    return mid


async def select_target(bot, chat_id, post, mid, rows=None):
    try:
        await bot.copy_message(chat_id, CHANNEL_ID, mid)  # показываем, какой это пост
    except Exception as e:
        logging.warning("Не удалось открыть пост %s: %s", mid, e)
        await bot.send_message(
            chat_id,
            f"Не получилось открыть пост #{mid}: {e}\n\n"
            "Проверьте номер и что бот добавлен админом канала.",
            reply_markup=pick_kb(load_state()),
        )
        return
    if rows:  # кнопки прочитаны из пересланного поста: это самые свежие данные
        remember_post(mid, None, rows)
        known, origin = rows, "прочитала из пересланного поста"
    else:
        known = load_state()["post_buttons"].get(str(mid))
        origin = "запомнила раньше" if known else None
    post.update(target=mid, step="menu", buttons=known or [], base_buttons=list(known or []),
                buttons_known=bool(known), append=False, text=None, plain="", media=None)
    if origin:
        info = f"Кнопки под постом ({origin}):\n{buttons_summary(known)}"
    else:
        info = ("Кнопок бот не видит. Если они под постом есть, перешлите мне этот пост "
                "из канала: так бот их прочитает и ничего не потеряет.")
    await bot.send_message(
        chat_id,
        f"👆 Это пост #{mid}: {msg_link(mid)}\n\n{info}\n\nЧто сделать?",
        reply_markup=EDIT_MENU_KB,
        link_preview_options=NO_PREVIEW,
    )


# ----- отправка и предпросмотр -----

def ctrl_kb(post):
    cancel = [InlineKeyboardButton("🗑 Отмена", callback_data="post:cancel")]
    txt = InlineKeyboardButton("✏️ Текст", callback_data="post:txt")
    btn = InlineKeyboardButton("🔘 Кнопки", callback_data="post:btn")
    if post["mode"] == "new":
        rows = [
            [InlineKeyboardButton("✅ Опубликовать", callback_data="post:pub")],
            [InlineKeyboardButton("🔕 Опубликовать без звука", callback_data="post:pubs")],
            [txt, btn],
            cancel,
        ]
    elif post["mode"] == "text":
        rows = [
            [InlineKeyboardButton("✅ Заменить текст в канале", callback_data="post:pub")],
            [txt, btn],
            cancel,
        ]
    else:
        rows = [
            [InlineKeyboardButton("✅ Применить кнопки в канале", callback_data="post:pub")],
            [btn],
            cancel,
        ]
    return InlineKeyboardMarkup(rows)


async def send_post(bot, target, post, kb, silent=False):
    media, text = post["media"], post["text"] or None
    common = dict(
        parse_mode=ParseMode.HTML, reply_markup=kb, disable_notification=silent
    )
    if media and media[0] == "photo":
        return await bot.send_photo(target, media[1], caption=text, **common)
    if media:
        return await bot.send_video(target, media[1], caption=text, **common)
    return await bot.send_message(target, post["text"], **common)


async def send_preview(bot, chat_id, post):
    kb = post_kb(post["buttons"])
    mode, mid = post["mode"], post.get("target")
    try:
        if mode == "buttons":  # показываем сам пост канала с новыми кнопками
            await bot.copy_message(chat_id, CHANNEL_ID, mid, reply_markup=kb)
        else:
            await send_post(bot, chat_id, post, kb)
    except Exception as e:
        logging.warning("Предпросмотр поста не получился: %s", e)
        await bot.send_message(
            chat_id,
            f"Не получилось показать пост: {e}\n\n"
            "Проверьте текст и ссылки в кнопках. Их можно поправить кнопками ниже.",
            reply_markup=ctrl_kb(post),
        )
        return
    if mode == "new":
        note = "👆 Так пост будет выглядеть в канале. Публикуем?"
    elif mode == "text":
        note = f"👆 Так будет выглядеть пост #{mid} после замены текста."
        if not post["buttons"] and not post.get("buttons_known"):
            note += (
                "\n\n⚠️ Бот не знает, какие кнопки у этого поста. Если они были, "
                "при замене текста пропадут: нажмите «Кнопки» и пришлите их заново."
            )
        note += "\n\nЗаменяем?"
    else:
        note = f"👆 Так будет выглядеть пост #{mid} с этими кнопками. Применяем?"
    await bot.send_message(chat_id, note, reply_markup=ctrl_kb(post))


async def apply_edit(bot, post):
    # Правка уже опубликованного поста: текст или только кнопки.
    mid, rows = post["target"], post["buttons"]
    kb = post_kb(rows)
    if post["mode"] == "text":
        try:
            await retry_call(
                bot.edit_message_text,
                post["text"], chat_id=CHANNEL_ID, message_id=mid,
                parse_mode=ParseMode.HTML, reply_markup=kb,
            )
        except BadRequest as e:
            low = str(e).lower()
            if "no text in the message" in low:  # у поста есть фото: правим подпись
                await retry_call(
                    bot.edit_message_caption,
                    chat_id=CHANNEL_ID, message_id=mid,
                    caption=post["text"], parse_mode=ParseMode.HTML, reply_markup=kb,
                )
            elif "not modified" not in low:
                raise
        remember_post(mid, post_title(post.get("plain"), None), rows)
    else:
        try:
            await retry_call(
                bot.edit_message_reply_markup,
                chat_id=CHANNEL_ID, message_id=mid,
                reply_markup=kb or InlineKeyboardMarkup([]),
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise
        remember_post(mid, None, rows)


EDIT_FAIL_HINT = (
    "Проверьте, что бот добавлен администратором канала с правами «Публикация "
    "сообщений» и «Изменение сообщений других»."
)


# ----- кнопки внизу чата -----

async def post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    context.application.bot_data["post"] = {
        "step": "content", "mode": "new", "target": None,
        "text": None, "plain": "", "media": None, "buttons": [], "buttons_known": True,
    }
    await update.message.reply_text(POST_START_TEXT, reply_markup=POST_CANCEL_KB)


async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    context.application.bot_data["post"] = {
        "step": "pick", "mode": "edit", "target": None,
        "text": None, "plain": "", "media": None, "buttons": [], "buttons_known": False,
    }
    await update.message.reply_text(PICK_TEXT, reply_markup=pick_kb(load_state()))


async def price_hint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data.pop("post", None)
    await update.message.reply_text(
        "💰 Пришлите текст прайса поставщика (можно несколькими сообщениями или "
        f"пересланным постом). Через {WAIT_SECONDS} сек. после последнего сообщения "
        "пришлю черновик, либо нажмите «Собрать сейчас»."
    )


async def reset_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🧹 Забыть старые посты прайса? Следующий прайс уйдёт в канал новыми постами "
        "(старые посты в канале останутся как есть).",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("Да, забыть", callback_data="menu:reset"),
                InlineKeyboardButton("Нет", callback_data="menu:no"),
            ]]
        ),
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    bd = context.application.bot_data
    had = bool(bd.get("post") or bd.get("draft") or bd.get("buf") or bd.get("route"))
    bd.pop("post", None)
    for job in context.job_queue.get_jobs_by_name("flush"):
        job.schedule_removal()
    reset_work(bd)
    await update.message.reply_text(
        "Отменено. В канал ничего не ушло." if had else "Сейчас нечего отменять.",
        reply_markup=MAIN_KB,
    )


async def on_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    # Нажатие кнопки с клавиатуры внизу чата. True, если это была такая кнопка.
    text = (update.message.text or "").strip()
    handlers = {
        BTN_NEW: post_cmd, BTN_EDIT: edit_cmd, BTN_PRICE: price_hint,
        BTN_RESET: reset_ask, BTN_CANCEL: cancel_cmd, BTN_HELP: start,
    }
    handler = handlers.get(text)
    if not handler:
        return False
    await handler(update, context)
    return True


async def on_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer()
        return
    await q.answer()
    if q.data == "menu:reset":
        state = load_state()
        state.update(ids=[], posts={}, menu_items=[])
        save_state(state)
        reset_work(context.application.bot_data)
        await q.edit_message_text("Ок, следующий прайс опубликую новыми постами.")
    else:
        await q.edit_message_text("Хорошо, ничего не меняю.")


# ----- ввод текста во время создания или правки поста -----

async def on_post_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    bd = context.application.bot_data
    post = bd["post"]
    step = post["step"]

    if step == "pick":
        mid = target_from_forward(msg)
        rows = None
        if isinstance(mid, int):  # пост переслан: читаем его кнопки
            rows, _skipped = buttons_from_markup(getattr(msg, "reply_markup", None))
        if mid is None and msg.text:
            mid = target_from_text(msg.text)
        if mid == "other":
            await msg.reply_text(
                "Это пост из другого канала. Пришлите пост из вашего канала.",
                reply_markup=pick_kb(load_state()),
            )
        elif mid is None:
            await msg.reply_text(
                "Не вижу поста. Перешлите сюда пост из канала, пришлите ссылку на него "
                "(t.me/имя_канала/123) или выберите из списка.",
                reply_markup=pick_kb(load_state()),
            )
        else:
            await select_target(context.bot, msg.chat_id, post, mid, rows)
        return

    if step == "menu":
        await msg.reply_text(
            "Выберите, что сделать с постом, кнопками под ним.", reply_markup=EDIT_MENU_KB
        )
        return

    if step == "delete":
        await msg.reply_text(
            "Нажимайте на кнопки под сообщением выше, затем «Готово».",
            reply_markup=delete_kb(post["buttons"]),
        )
        return

    if step == "content":
        media, body, plain = None, "", ""
        if msg.photo:
            media = ("photo", msg.photo[-1].file_id)
            body, plain = msg.caption_html or "", msg.caption or ""
        elif msg.video:
            media = ("video", msg.video.file_id)
            body, plain = msg.caption_html or "", msg.caption or ""
        elif msg.text:
            body, plain = msg.text_html, msg.text
        else:
            await msg.reply_text(
                "Пришлите текст, фото или видео (текст тогда пишите в подписи).",
                reply_markup=POST_CANCEL_KB,
            )
            return
        if post["mode"] == "new" and media and len(plain) > CAPTION_LIMIT:
            await msg.reply_text(
                f"Подпись к фото или видео не может быть длиннее {CAPTION_LIMIT} "
                f"символов, у вас {len(plain)}. Сократите текст или пришлите пост без фото.",
                reply_markup=POST_CANCEL_KB,
            )
            return
        if len(plain) > POST_TEXT_LIMIT:
            await msg.reply_text(
                f"Пост не может быть длиннее {POST_TEXT_LIMIT} символов, у вас {len(plain)}.",
                reply_markup=POST_CANCEL_KB,
            )
            return
        if not media and not plain.strip():
            await msg.reply_text("Текст пустой. Пришлите текст поста.",
                                 reply_markup=POST_CANCEL_KB)
            return
        post["text"], post["plain"] = body, plain
        if post["mode"] == "new":
            post["media"] = media
            if post.pop("editing", False):  # правили текст: кнопки остаются прежние
                post["step"] = "ready"
                await send_preview(context.bot, msg.chat_id, post)
            else:
                post["step"] = "buttons"
                await msg.reply_text(POST_BUTTONS_TEXT, reply_markup=POST_BUTTONS_KB)
        else:  # правка текста поста, который уже в канале
            post["media"] = None
            if media:
                await msg.reply_text(
                    "Фото или видео в опубликованном посте поменять нельзя, заменю только текст."
                )
            post["step"] = "ready"
            await send_preview(context.bot, msg.chat_id, post)

    elif step == "buttons":
        if not msg.text:
            await msg.reply_text(
                "Нужен текст с кнопками: название - ссылка (по одной на строку). "
                "Или нажмите кнопку под сообщением выше.",
                reply_markup=buttons_prompt(post)[1],
            )
            return
        rows, bad = parse_buttons(msg.text)
        if bad or not rows:
            await msg.reply_text(
                "Не поняла эти строки:\n" + "\n".join(bad[:5] or [msg.text[:100]])
                + "\n\nНужно так: название - ссылка (можно добавить - style:green, blue или red). "
                "Пришлите кнопки ещё раз.",
                reply_markup=buttons_prompt(post)[1],
            )
            return
        base = list(post.get("base_buttons") or []) if post.get("append") else []
        post["buttons"], post["buttons_known"] = base + rows, True
        post.pop("editing", None)
        post["step"] = "ready"
        await send_preview(context.bot, msg.chat_id, post)

    else:  # ready
        await msg.reply_text(
            "Пост готов. Нажмите нужную кнопку под предпросмотром.",
            reply_markup=ctrl_kb(post),
        )


# ----- нажатия на кнопки под сообщениями бота -----

async def on_post_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer()
        return
    await q.answer()
    bd = context.application.bot_data
    chat_id = q.message.chat_id
    action = q.data[len("post:"):]
    post = bd.get("post")

    if action == "cancel":
        bd.pop("post", None)
        await q.edit_message_text("Отменено. В канал ничего не ушло.")
        return
    if not post:
        await q.edit_message_text("Это уже неактуально (бот перезапускался). Начните заново кнопками внизу.")
        return

    if action.startswith("pick:"):
        if post["step"] not in ("pick", "menu"):
            return
        mid = int(action[5:])
        await q.edit_message_text(f"Выбран пост #{mid}.")
        await select_target(context.bot, chat_id, post, mid)

    elif action == "repick":
        post.update(step="pick", mode="edit", target=None)
        await q.edit_message_text(PICK_TEXT, reply_markup=pick_kb(load_state()))

    elif action == "etxt":
        if post["step"] != "menu":
            return
        post.update(step="content", mode="text")
        await q.edit_message_text(
            "✏️ Пришлите новый текст поста целиком. Жирный, курсив и ссылки сохранятся."
            + ("\nКнопки поста останутся прежние." if post["buttons"] else "")
        )

    elif action == "ebtn":
        if post["step"] != "menu":
            return
        post.update(step="buttons", mode="buttons", text=None, append=False)
        await q.edit_message_text(POST_BUTTONS_TEXT, reply_markup=POST_BUTTONS_KB)

    elif action == "eadd":
        if post["step"] != "menu":
            return
        post.update(step="buttons", mode="buttons", text=None, append=True)
        await q.edit_message_text(ADD_BUTTONS_TEXT, reply_markup=ADD_KB)

    elif action == "edel":
        if post["step"] != "menu":
            return
        if not post["buttons"]:
            await q.edit_message_text(
                "Кнопок, которые бот знает, у этого поста нет. Если они есть, перешлите "
                "мне этот пост из канала.",
                reply_markup=EDIT_MENU_KB,
            )
            return
        post.update(step="delete", mode="buttons", text=None, append=False)
        await q.edit_message_text(DELETE_TEXT, reply_markup=delete_kb(post["buttons"]))

    elif action.startswith("rm:"):
        if post["step"] != "delete":
            return
        post["buttons"] = remove_button(post["buttons"], int(action[3:]))
        post["buttons_known"] = True
        await q.edit_message_text(
            DELETE_TEXT + ("" if post["buttons"] else "\n\nКнопок не осталось."),
            reply_markup=delete_kb(post["buttons"]),
        )

    elif action == "rmdone":
        if post["step"] != "delete":
            return
        post["step"] = "ready"
        await q.edit_message_text("Готово, смотрим, что получилось.")
        await send_preview(context.bot, chat_id, post)

    elif action == "erm":
        if post["step"] != "menu":
            return
        post.update(mode="buttons", buttons=[], text=None)
        try:
            await apply_edit(context.bot, post)
        except Exception as e:
            logging.warning("Не удалось убрать кнопки: %s", e)
            await q.edit_message_text(
                f"Не получилось убрать кнопки: {e}\n\n{EDIT_FAIL_HINT}",
                reply_markup=EDIT_MENU_KB,
            )
            return
        bd.pop("post", None)
        await q.edit_message_text(
            f"✅ Кнопки убраны: {msg_link(post['target'])}", link_preview_options=NO_PREVIEW
        )

    elif action == "nobtn":
        if post["step"] != "buttons":
            return
        if post.get("append"):  # «Не добавлять»: старые кнопки остаются как были
            post["buttons"] = list(post.get("base_buttons") or [])
            note = "Ничего не добавляю."
        else:
            post["buttons"] = []
            note = "Без кнопок."
        post["buttons_known"] = True
        post.pop("editing", None)
        post["step"] = "ready"
        await q.edit_message_text(note)
        await send_preview(context.bot, chat_id, post)

    elif action == "txt":
        if post["step"] != "ready":
            return
        post["step"], post["editing"] = "content", True
        await q.edit_message_text(
            "Пришлите новый текст (или фото/видео с подписью). Кнопки останутся прежние."
        )

    elif action == "btn":
        if post["step"] != "ready":
            return
        post["step"], post["editing"] = "buttons", True
        if post["mode"] != "buttons":
            post["append"] = False
        text, kb = buttons_prompt(post)
        await q.edit_message_text(text, reply_markup=kb)

    elif action in ("pub", "pubs"):
        if post["step"] != "ready":
            return
        if post["mode"] == "new":
            try:
                sent = await send_post(
                    context.bot, CHANNEL_ID, post, post_kb(post["buttons"]),
                    silent=(action == "pubs"),
                )
            except Exception as e:
                logging.warning("Не удалось опубликовать пост: %s", e)
                await q.edit_message_text(
                    f"Не получилось опубликовать: {e}\n\n{EDIT_FAIL_HINT} "
                    "Пост сохранён: можно нажать «Опубликовать» ещё раз.",
                    reply_markup=ctrl_kb(post),
                )
                return
            remember_post(sent.message_id, post_title(post["plain"], post["media"]), post["buttons"])
            bd.pop("post", None)
            await q.edit_message_text(
                f"✅ Опубликовано: {msg_link(sent.message_id)}",
                link_preview_options=NO_PREVIEW,
            )
        else:
            try:
                await apply_edit(context.bot, post)
            except Exception as e:
                logging.warning("Не удалось отредактировать пост: %s", e)
                await q.edit_message_text(
                    f"Не получилось изменить пост: {e}\n\n{EDIT_FAIL_HINT} "
                    "Можно нажать кнопку ещё раз.",
                    reply_markup=ctrl_kb(post),
                )
                return
            bd.pop("post", None)
            await q.edit_message_text(
                f"✅ Готово, пост обновлён: {msg_link(post['target'])}",
                link_preview_options=NO_PREVIEW,
            )


async def on_startup(app):
    # меню команд рядом с полем ввода (кнопка «Меню»)
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("start", "Главное меню"),
                BotCommand("post", "Новый пост"),
                BotCommand("edit", "Править пост в канале"),
                BotCommand("cancel", "Отмена"),
            ]
        )
    except Exception as e:
        logging.warning("Не удалось задать меню команд: %s", e)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    logging.error("Ошибка в боте", exc_info=context.error)
    try:
        await context.bot.send_message(
            OWNER_ID,
            f"Ошибка: {type(context.error).__name__}: {context.error}",
        )
    except Exception:
        pass


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .connect_timeout(30)  # по умолчанию всего 5 секунд
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new))
    app.add_handler(CommandHandler("raw", raw_cmd))
    app.add_handler(CommandHandler("post", post_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("edit", edit_cmd))
    app.add_handler(CallbackQueryHandler(on_menu_button, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(on_post_button, pattern=r"^post:"))
    app.add_handler(CallbackQueryHandler(on_route, pattern=r"^r:"))
    app.add_handler(
        CallbackQueryHandler(on_button, pattern=r"^(now|pub|edit|cancel)$")
    )
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_message)
    )
    app.run_polling()


if __name__ == "__main__":
    main()

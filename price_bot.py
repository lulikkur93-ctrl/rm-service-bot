"""
Бот для подготовки и публикации прайса в канале (версия 14).

Как пользоваться:
1. Вставьте боту текст прайса (можно несколькими сообщениями).
2. Бот ждёт 1 минуту после последнего сообщения (или нажмите «Собрать сейчас»).
3. Бот присылает черновик: лишнее убрано, наценка добавлена, модели выделены.
4. Нажмите «Опубликовать». Если в канале уже есть посты с прайсом, бот СПРОСИТ,
   какой из них заменить (⭐ подсказка по названию модели), или создать новый пост,
   или пропустить. Кнопка «Автоподбор» подбирает посты сама по названию модели.
5. Бот ведёт в канале пост-меню с кнопками: названия iPhone (ведут к нужному
   посту), «Заказать», «Ремонт», «О нас». Старые кнопки сохраняются.

Переменные окружения (Railway -> Variables):
  BOT_TOKEN              токен ОТДЕЛЬНОГО бота для прайса
  OWNER_ID               ваш Telegram ID (число), только вы можете управлять ботом
  CHANNEL_ID             @имя_канала или -100xxxxxxxxxx
  MARKUP                 наценка в рублях, по умолчанию 5000
  MIN_PRICE_FOR_MARKUP   наценка только на цены от этой суммы, по умолчанию 30000
  WAIT_SECONDS           сколько ждать новые части текста, по умолчанию 60
  COMBINE_GENERATIONS    поколения iPhone, которые идут ОДНИМ постом (по умолчанию 13,14,15);
                         остальные модели: одна модель = один пост
  ORDER_URL              куда ведёт кнопка «Заказать» (@имя, t.me/... или https://...)
  REPAIR_URL             куда ведёт кнопка «Ремонт»
  ABOUT_URL              куда ведёт кнопка «О нас» (например, ссылка на пост)
  MENU_TEXT              текст пост-меню (по умолчанию: выбрать модель или раздел)
  STATE_PATH             файл состояния (по умолчанию state.json; с Volume в Railway
                         укажите, например, /data/state.json, тогда ничего не теряется)
  PRICE_MESSAGE_IDS      id постов с прайсом через запятую (бот подскажет сам)
  MENU_MESSAGE_ID        id поста-меню (бот подскажет сам)
"""
import html
import json
import logging
import os
import re

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
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
VERSION = "14"
LIMIT = 3800  # лимит Telegram 4096, берём с запасом (теги тоже считаем)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


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
    for x in os.getenv("COMBINE_GENERATIONS", "13,14,15").split(",")
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
            key = "legacy" if mdl["legacy"] else mdl["title"]
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"subs": {}, "legacy": mdl["legacy"]}
                order.append(key)
            sub = g["subs"].setdefault(mdl["title"], {"model": mdl, "lines": []})
            sub["lines"].append(line)
        if rest:
            other.append((heads, rest))

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
            group_title[gid] = subs[0]["model"]["title"]
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
    base = {"ids": [], "menu": None, "posts": {}, "menu_items": []}
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
        "Вставьте текст прайса поставщика (можно несколькими сообщениями). "
        f"Через {WAIT_SECONDS} сек. после последнего сообщения (или по кнопке) "
        "пришлю черновик. В канал ничего не уходит, пока вы не нажмёте "
        "«Опубликовать». Перед публикацией бот спросит, какой из постов в канале "
        "заменить. При публикации бот ведёт в канале пост с кнопками: "
        "названия iPhone, «Заказать», «Ремонт», «О нас».\n\n"
        "/new: забыть старые посты (следующий прайс будет новыми постами)."
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
    if context.application.bot_data.pop("raw_mode", False):
        await send_outline(msg)
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
    res = await publish_routed(bot, draft, mapping)

    lines = []
    for j, (mid, kind) in sorted(res["actual"].items()):
        title = draft["post_titles"][j]
        word = {"edit": "заменён", "new": "новый"}[kind]
        lines.append(f"Пост {j + 1} ({title}): {word} #{mid}")
    for j in range(len(draft["posts"])):
        if j not in res["actual"]:
            lines.append(f"Пост {j + 1} ({draft['post_titles'][j]}): пропущен")
    reply = f"✅ Готово. Кнопок моделей в меню: {res['buttons']}.\n\n" + "\n".join(lines)

    env_lines = []
    if res["ids"] != res["old_ids"]:
        env_lines.append(f"PRICE_MESSAGE_IDS={','.join(map(str, res['ids']))}")
    if res["menu_id"] != res["old_menu"]:
        env_lines.append(f"MENU_MESSAGE_ID={res['menu_id']}")
    if env_lines:
        reply += (
            "\n\nДобавьте в Railway переменные, чтобы id не потерялись "
            "после перезапуска:\n" + "\n".join(env_lines)
        )
    missing = [(t, e) for t, e, u in STATIC_BUTTONS if not u]
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

    if menu_id is None:
        # меню создаём первым, чтобы оно было выше прайса
        sent = await bot.send_message(CHANNEL_ID, MENU_TEXT)
        menu_id = sent.message_id

    actual = {}
    for j, text in enumerate(draft["posts"]):
        target = mapping.get(j, "new")
        if target == "skip":
            continue
        meta = {"title": draft["post_titles"][j], "labels": draft["post_labels"][j]}
        if target != "new":
            mid = int(target)
            try:
                await bot.edit_message_text(
                    text,
                    chat_id=CHANNEL_ID,
                    message_id=mid,
                    parse_mode=ParseMode.HTML,
                )
                actual[j] = (mid, "edit")
                posts_meta[str(mid)] = meta
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
    kb = build_menu_kb(menu_list)
    try:
        await bot.edit_message_text(
            MENU_TEXT, chat_id=CHANNEL_ID, message_id=menu_id, reply_markup=kb
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
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new))
    app.add_handler(CommandHandler("raw", raw_cmd))
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

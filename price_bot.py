"""
Бот для подготовки и публикации прайса в канале (версия 6).

Как пользоваться:
1. Вставьте боту текст прайса (можно несколькими сообщениями).
2. Бот ждёт 1 минуту после последнего сообщения (или нажмите «Собрать сейчас»).
3. Бот присылает черновик: лишнее убрано, наценка добавлена, модели выделены.
4. Под черновиком кнопки: «Опубликовать», «Изменить», «Отмена».
   В канал ничего не уходит, пока вы не нажмёте «Опубликовать».

Переменные окружения (Railway -> Variables):
  BOT_TOKEN              токен ОТДЕЛЬНОГО бота для прайса
  OWNER_ID               ваш Telegram ID (число), только вы можете управлять ботом
  CHANNEL_ID             @имя_канала или -100xxxxxxxxxx
  MARKUP                 наценка в рублях, по умолчанию 5000
  MIN_PRICE_FOR_MARKUP   наценка только на цены от этой суммы, по умолчанию 30000
  WAIT_SECONDS           сколько ждать новые части текста, по умолчанию 60
  PRICE_MESSAGE_IDS      id опубликованных постов через запятую (бот подскажет сам)
"""
import html
import json
import logging
import os
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
STATE_FILE = "state.json"
VERSION = "7"
LIMIT = 3800  # лимит Telegram 4096, берём с запасом (теги тоже считаем)

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


def build_posts(blocks):
    """Склеивает блоки в посты до LIMIT. Режем только по границе блока
    (а слишком длинный блок по строкам, повторяя заголовок)."""
    rendered = []
    for heads, phones in blocks:
        r = render_block(heads, phones)
        if len(r) <= LIMIT or not phones:
            rendered.append(r)
            continue
        cur = []
        for p in phones:
            if cur and len(render_block(heads, cur + [p])) > LIMIT:
                rendered.append(render_block(heads, cur))
                cur = [p]
            else:
                cur.append(p)
        if cur:
            rendered.append(render_block(heads, cur))

    posts, cur = [], ""
    for r in rendered:
        if cur and len(cur) + 2 + len(r) > LIMIT:
            posts.append(cur)
            cur = r
        else:
            cur = f"{cur}\n\n{r}" if cur else r
    if cur:
        posts.append(cur)
    return posts


# ---------- состояние ----------

def load_ids():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)["ids"]
    except Exception:
        env = os.getenv("PRICE_MESSAGE_IDS", "")
        return [int(x) for x in env.split(",") if x.strip()]


def save_ids(ids):
    with open(STATE_FILE, "w") as f:
        json.dump({"ids": ids}, f)


def reset_work(bd):
    for key in ("buf", "draft", "edit_mode"):
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
        "«Опубликовать».\n\n"
        "/new: опубликовать прайс новыми постами (старые id забыть)."
    )


async def new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    save_ids([])
    reset_work(context.application.bot_data)
    await update.message.reply_text(
        "Ок, следующий прайс опубликую новыми постами."
    )


# ---------- приём текста ----------

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or update.effective_user.id != OWNER_ID:
        return
    text = msg.text or msg.caption
    logging.info("Сообщение: есть текст=%s, символов=%s", bool(text), len(text or ""))
    if not text:
        await msg.reply_text(
            "В этом сообщении нет текста. Нужен текст прайса: скопируйте его "
            "из поста поставщика и вставьте сюда."
        )
        return
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
    posts = build_posts(blocks)
    bd["draft"] = {"posts": posts}

    if edit_mode:
        head = f"Черновик по вашему тексту: постов {len(posts)}."
    else:
        head = (
            f"Черновик: постов {len(posts)}, цен с наценкой {changed}, "
            f"убрано строк {removed}."
        )
    if suspicious:
        head += "\n\nПохоже на цену, но не распознано (проверьте):\n"
        head += "\n".join(suspicious[:5])
    await bot.send_message(chat_id, head)
    for post in posts:
        await bot.send_message(chat_id, post, parse_mode=ParseMode.HTML)
    await bot.send_message(
        chat_id, "Публикуем в канал?", reply_markup=DRAFT_KB
    )


# ---------- кнопки ----------

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
        await q.edit_message_text("Публикую…")
        new_ids, old_ids = await publish(context.bot, draft["posts"])
        bd.pop("draft", None)
        reply = f"✅ Опубликовано постов: {len(draft['posts'])}."
        if new_ids != old_ids:
            reply += (
                "\n\nДобавьте в Railway переменную, чтобы id не потерялись "
                f"после перезапуска:\nPRICE_MESSAGE_IDS={','.join(map(str, new_ids))}"
            )
        await context.bot.send_message(chat_id, reply)


async def publish(bot, posts):
    ids = load_ids()
    new_ids = []
    for i, part in enumerate(posts):
        if i < len(ids):
            try:
                await bot.edit_message_text(
                    part,
                    chat_id=CHANNEL_ID,
                    message_id=ids[i],
                    parse_mode=ParseMode.HTML,
                )
                new_ids.append(ids[i])
                continue
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    new_ids.append(ids[i])
                    continue
                logging.warning("Не удалось отредактировать %s: %s", ids[i], e)
        sent = await bot.send_message(CHANNEL_ID, part, parse_mode=ParseMode.HTML)
        new_ids.append(sent.message_id)

    # если прайс стал короче, лишние старые посты удаляем
    for old in ids[len(posts):]:
        try:
            await bot.delete_message(CHANNEL_ID, old)
        except BadRequest:
            pass
    save_ids(new_ids)
    return new_ids, ids


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
    app.add_handler(
        CallbackQueryHandler(on_button, pattern=r"^(now|pub|edit|cancel)$")
    )
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_message)
    )
    app.run_polling()


if __name__ == "__main__":
    main()

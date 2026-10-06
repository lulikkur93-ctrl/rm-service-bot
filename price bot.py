"""
Бот для обновления прайса в канале.

Как пользоваться:
1. Перешлите боту (в личку) посты с ценами из канала поставщика.
   Если прайс пришёл несколькими сообщениями, пересылайте подряд.
2. Через 3 секунды бот уберёт лишнее (зарядки, адаптеры, гарантии, asis),
   добавит наценку и обновит посты в вашем канале.

Переменные окружения (Railway -> Variables):
  BOT_TOKEN              токен ОТДЕЛЬНОГО бота для прайса (не того, что принимает заявки)
  OWNER_ID               ваш Telegram ID (число), только вы сможете управлять ботом
  CHANNEL_ID             @имя_канала или -100xxxxxxxxxx
  MARKUP                 наценка в рублях, по умолчанию 5000
  MIN_PRICE_FOR_MARKUP   наценка только на цены от этой суммы, по умолчанию 30000
  PRICE_MESSAGE_IDS      id постов с прайсом через запятую (бот сам подскажет после первого запуска)
"""
import json
import logging
import os
import re

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
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
STATE_FILE = "state.json"
LIMIT = 4000  # лимит Telegram на сообщение 4096, берём с запасом

# "18 Pro 256Gb Black-124.000🇰🇷 🇭🇰 (1 sim+e sim)" -> название, цена, хвост
PRICE_RE = re.compile(r"^(.*?)-(\d{1,3}(?:\.\d{3})+)(?!\d)(.*)$")

# Что НЕ копируем: зарядки, блоки/адаптеры, чехлы, дополнительная гарантия,
# позиции asis, строки "От 10шт ..." (оптовые цены на аксессуары),
# строки "+3 месяца -3.000" (платная гарантия).
EXCLUDE_RE = re.compile(
    r"заряд|чехол|адаптер|adapter|блок"
    r"|\bas[\s\-]?is\b"
    r"|(?:платн|доп)\w*\.?\s*гаранти|applecare"
    r"|\+\s*\d+\s*мес"
    r"|^\s*от\s+\d+",
    re.IGNORECASE,
)


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def convert(text: str):
    """Возвращает: готовый текст, сколько цен изменено, сколько строк убрано,
    список подозрительных строк."""
    lines = text.split("\n")
    kinds, new_lines = [], []
    changed = 0

    for line in lines:
        if not line.strip():
            kinds.append("blank")
            new_lines.append(line)
            continue
        excluded = bool(EXCLUDE_RE.search(line))
        m = PRICE_RE.match(line)
        if excluded:
            kinds.append("drop")
            new_lines.append(line)
        elif m:
            price = int(m.group(2).replace(".", ""))
            if price >= MIN_PRICE:
                price += MARKUP
                changed += 1
            kinds.append("phone")
            new_lines.append(f"{m.group(1)}-{fmt(price)}{m.group(3)}")
        else:
            kinds.append("text")
            new_lines.append(line)

    # Идём снизу вверх: строки с ценой оставляем, а строки без цены (заголовки)
    # оставляем только если они стоят прямо над оставленными ценами
    # (не больше двух заголовков подряд). Так пропадают описания и хвосты.
    keep = [False] * len(lines)
    dist = 2
    for i in range(len(lines) - 1, -1, -1):
        k = kinds[i]
        if k == "phone":
            keep[i] = True
            dist = 0
        elif k == "drop":
            dist = 2
        elif k == "text":
            if dist < 2:
                keep[i] = True
                dist += 1

    out, removed, suspicious = [], 0, []
    for i, line in enumerate(new_lines):
        if kinds[i] == "blank":
            if out and out[-1].strip():
                out.append("")
            continue
        if keep[i]:
            out.append(line)
            if kinds[i] == "text" and re.search(r"\d\.\d{3}", line):
                suspicious.append(line)
        else:
            removed += 1
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out), changed, removed, suspicious


def chunks(text: str):
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > LIMIT:
            parts.append(cur.rstrip("\n"))
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur.rstrip("\n"))
    return parts


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await update.message.reply_text(
        "Перешлите мне прайс поставщика, я уберу лишнее, добавлю наценку "
        f"{fmt(MARKUP)} ₽ и обновлю посты в канале.\n\n"
        "/new — опубликовать прайс новыми постами (старые id забыть)."
    )


async def new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    save_ids([])
    await update.message.reply_text(
        "Ок, следующий прайс опубликую новыми постами."
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or update.effective_user.id != OWNER_ID:
        return
    text = msg.text or msg.caption
    if not text:
        return
    buf = context.application.bot_data.setdefault("buf", [])
    first = not buf
    buf.append(text)
    if first:
        await msg.reply_text("Принял. Если будут ещё части, пересылайте сейчас.")
    for job in context.job_queue.get_jobs_by_name("flush"):
        job.schedule_removal()
    context.job_queue.run_once(flush, 3, chat_id=msg.chat_id, name="flush")


async def on_other(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # сообщения без текста (например, картинка без подписи) просто пропускаем
    logging.info("Сообщение без текста пропущено")


async def flush(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    buf = context.application.bot_data.pop("buf", [])
    if not buf:
        return

    text, changed, removed, suspicious = convert("\n".join(buf))
    if changed == 0:
        await context.bot.send_message(
            chat_id,
            "Цены вида «название-124.000» не найдены. "
            "Проверьте, что переслан прайс.",
        )
        return

    parts = chunks(text)
    ids = load_ids()
    new_ids = []
    for i, part in enumerate(parts):
        if i < len(ids):
            try:
                await context.bot.edit_message_text(
                    part, chat_id=CHANNEL_ID, message_id=ids[i]
                )
                new_ids.append(ids[i])
                continue
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    new_ids.append(ids[i])
                    continue
                logging.warning("Не удалось отредактировать %s: %s", ids[i], e)
        sent = await context.bot.send_message(CHANNEL_ID, part)
        new_ids.append(sent.message_id)

    # если прайс стал короче, лишние старые посты удаляем
    for old in ids[len(parts):]:
        try:
            await context.bot.delete_message(CHANNEL_ID, old)
        except BadRequest:
            pass

    save_ids(new_ids)

    reply = (
        f"Готово: обновлено цен {changed}, убрано строк {removed}, "
        f"постов {len(parts)}."
    )
    if new_ids != ids:
        reply += (
            "\n\nДобавьте в Railway переменную, чтобы id не потерялись "
            f"после перезапуска:\nPRICE_MESSAGE_IDS={','.join(map(str, new_ids))}"
        )
    if suspicious:
        reply += "\n\nПохоже на цену, но не распознано (проверьте вручную):\n"
        reply += "\n".join(suspicious[:5])
    await context.bot.send_message(chat_id, reply)


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
        MessageHandler(
            filters.ChatType.PRIVATE
            & ((filters.TEXT & ~filters.COMMAND) | filters.CAPTION),
            on_text,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND, on_other
        )
    )
    app.run_polling()


if __name__ == "__main__":
    main()

"""
Бот для обновления прайса в канале.

Как пользоваться:
1. Перешлите боту (в личку) пост с ценами из канала поставщика.
   Если прайс пришёл несколькими сообщениями, пересылайте подряд.
2. Через 3 секунды бот добавит наценку и обновит посты в вашем канале.

Переменные окружения (Railway -> Variables):
  BOT_TOKEN              токен ОТДЕЛЬНОГО бота для прайса (не того, что принимает заявки)
  OWNER_ID               ваш Telegram ID (число), только вы сможете управлять ботом
  CHANNEL_ID             @имя_канала или -100xxxxxxxxxx
  MARKUP                 наценка в рублях, по умолчанию 5000
  MIN_PRICE_FOR_MARKUP   наценка только на цены от этой суммы, по умолчанию 30000
                         (чтобы не прибавлять 5000 к зарядкам и аксессуарам)
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

# Что НЕ копируем в ваш прайс: зарядки, чехлы, дополнительная гарантия, позиции asis.
# Если слово есть в заголовке раздела (строка без цены), пропускается весь раздел
# до следующего заголовка. Если в строке с ценой, пропускается только она.
EXCLUDE_RE = re.compile(
    r"заряд|чехол|\bas[\s\-]?is\b|(?:платн|доп)\w*\.?\s*гаранти|applecare",
    re.IGNORECASE,
)


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def convert(text: str):
    out, changed, removed, suspicious = [], 0, 0, []
    skipping = False
    for line in text.split("\n"):
        m = PRICE_RE.match(line)
        if not m and line.strip():
            # строка без цены считается заголовком раздела
            skipping = bool(EXCLUDE_RE.search(line))
            if skipping:
                removed += 1
                continue
        if m:
            if skipping or EXCLUDE_RE.search(line):
                removed += 1
                continue
            price = int(m.group(2).replace(".", ""))
            if price >= MIN_PRICE:
                price += MARKUP
                changed += 1
            line = f"{m.group(1)}-{fmt(price)}{m.group(3)}"
        elif re.search(r"\d\.\d{3}", line):
            suspicious.append(line)
        out.append(line)
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    return result, changed, removed, suspicious


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
        "Перешлите мне прайс поставщика, я добавлю наценку "
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
    context.application.bot_data.setdefault("buf", []).append(text)
    for job in context.job_queue.get_jobs_by_name("flush"):
        job.schedule_removal()
    context.job_queue.run_once(flush, 3, chat_id=msg.chat_id, name="flush")


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
        f"Готово: обновлено цен {changed}, пропущено строк {removed}, "
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


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("new", new))
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & ((filters.TEXT & ~filters.COMMAND) | filters.CAPTION),
            on_text,
        )
    )
    app.run_polling()


if __name__ == "__main__":
    main()

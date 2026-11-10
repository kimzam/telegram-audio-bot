
import asyncio
import logging
import os
import sqlite3
from html import escape

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)

# ---------- Настройки из переменных окружения ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = os.getenv("GROUP_ID")          # например: -1001234567890
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS")    # например: 111111111,222222222

LINK_TEXT = os.getenv("LINK_TEXT", "Музыка ИИ")
LINK_URL = os.getenv("LINK_URL", "https://t.me/your_channel")

# Стиль подписи: quote | bold | plain
CAPTION_STYLE = os.getenv("CAPTION_STYLE", "plain").strip().lower()

# Файл базы с уже опубликованными треками
DB_PATH = os.getenv("DB_PATH", "tracks.db")

# Удалять ли дубликаты, выложенные в группе другими людьми (нужны права админа)
DELETE_DUPLICATES = os.getenv("DELETE_DUPLICATES", "false").strip().lower() in (
    "1", "true", "yes", "on",
)

# Порт для веб-сервера (Render задаёт PORT сам; по умолчанию 10000)
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN or not GROUP_ID or not ADMIN_IDS_RAW:
    raise RuntimeError("Не заданы переменные окружения: BOT_TOKEN, GROUP_ID, ADMIN_IDS")

GROUP_ID = int(GROUP_ID)
ADMIN_IDS = {
    int(x)
    for x in ADMIN_IDS_RAW.replace(";", ",").replace(" ", ",").split(",")
    if x.strip()
}
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS пуст: укажите хотя бы один Telegram ID")

logging.basicConfig(level=logging.INFO)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# ---------- База опубликованных треков ----------
db = sqlite3.connect(DB_PATH)
db.execute(
    """CREATE TABLE IF NOT EXISTS tracks (
        unique_id    TEXT PRIMARY KEY,
        meta_key     TEXT,
        group_msg_id INTEGER NOT NULL
    )"""
)
db.execute("CREATE INDEX IF NOT EXISTS idx_meta ON tracks(meta_key)")
db.commit()


def norm(s: str | None) -> str:
    return " ".join((s or "").lower().split())


def extract_media(m: Message) -> dict | None:
    """Достаёт из сообщения всё нужное для отправки и поиска дубликатов."""
    if m.audio:
        a = m.audio
        meta = None
        if a.title or a.performer:
            meta = f"{norm(a.performer)}|{norm(a.title)}|{a.duration}"
        return dict(
            kind="audio", file_id=a.file_id, unique_id=a.file_unique_id,
            meta_key=meta, title=a.title, performer=a.performer, duration=a.duration,
        )
    if m.document and (m.document.mime_type or "").startswith("audio/"):
        d = m.document
        meta = f"{norm(d.file_name)}|{d.file_size}" if d.file_name else None
        return dict(
            kind="document", file_id=d.file_id, unique_id=d.file_unique_id, meta_key=meta,
        )
    if m.voice:
        v = m.voice
        return dict(kind="voice", file_id=v.file_id, unique_id=v.file_unique_id, meta_key=None)
    return None


def find_duplicate(media: dict) -> int | None:
    """Возвращает id сообщения в группе, если такой трек уже был."""
    row = db.execute(
        "SELECT group_msg_id FROM tracks "
        "WHERE unique_id = ? OR (meta_key IS NOT NULL AND meta_key = ?)",
        (media["unique_id"], media["meta_key"]),
    ).fetchone()
    return row[0] if row else None


def save_track(media: dict, group_msg_id: int) -> None:
    db.execute(
        "INSERT OR IGNORE INTO tracks (unique_id, meta_key, group_msg_id) VALUES (?, ?, ?)",
        (media["unique_id"], media["meta_key"], group_msg_id),
    )
    db.commit()


def post_link(msg_id: int) -> str | None:
    s = str(GROUP_ID)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{msg_id}"
    return None


def where_html(msg_id: int) -> str:
    url = post_link(msg_id)
    return f'<a href="{url}">вот здесь</a>' if url else "в группе выше"


# ---------- Подпись ----------
def build_caption() -> str:
    link = f'<a href="{escape(LINK_URL, quote=True)}">{escape(LINK_TEXT)}</a>'
    styles = {
        "plain": f"🎵 {link}",
        "bold": f"🎶 <b>{link}</b> 🎶",
        "quote": f"<blockquote>🎧 <b>{link}</b> ✨</blockquote>",
    }
    return styles.get(CAPTION_STYLE, styles["plain"])


# ---------- Публикация в группу ----------
async def publish(media: dict) -> Message:
    caption = build_caption()
    if media["kind"] == "audio":
        sent = await bot.send_audio(
            GROUP_ID, media["file_id"], caption=caption,
            title=media["title"], performer=media["performer"], duration=media["duration"],
        )
    elif media["kind"] == "document":
        sent = await bot.send_document(GROUP_ID, media["file_id"], caption=caption)
    else:
        sent = await bot.send_voice(GROUP_ID, media["file_id"], caption=caption)

    if media["kind"] != "voice":
        save_track(media, sent.message_id)
    return sent


# ---------- Роутеры ----------
AUDIO_DOC = F.document & F.document.mime_type.startswith("audio/")

# Личка: только администраторы, остальные игнорируются
private = Router()
private.message.filter(F.chat.type == "private", F.from_user.id.in_(ADMIN_IDS))
private.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

# Группа: только заданная группа
group = Router()
group.message.filter(F.chat.id == GROUP_ID)

# Треки, ожидающие решения «отправить всё равно»
pending: dict[str, dict] = {}


@private.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer("Привет! Отправь мне аудиофайл, и я перешлю его в группу.")


@private.message(F.audio | F.voice | AUDIO_DOC)
async def on_private_media(message: Message):
    media = extract_media(message)
    if media is None:
        return

    first_id = find_duplicate(media) if media["kind"] != "voice" else None
    if first_id is not None:
        if len(pending) > 200:
            pending.pop(next(iter(pending)))
        pending[f"{message.chat.id}:{message.message_id}"] = media
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Отправить всё равно",
                callback_data=f"force:{message.message_id}",
            )
        ]])
        await message.answer(
            f"⚠️ Дубликат! Этот трек уже публиковали — {where_html(first_id)}.\n"
            "В группу не отправлено.",
            reply_markup=kb,
            link_preview_options=NO_PREVIEW,
        )
        return

    try:
        await publish(media)
        await message.answer("✅ Отправлено в группу.")
    except Exception as e:
        logging.exception("Ошибка при отправке")
        await message.answer(f"❌ Не удалось отправить: {escape(str(e))}")


@private.callback_query(F.data.startswith("force:"))
async def on_force(cb: CallbackQuery):
    key = f"{cb.message.chat.id}:{cb.data.split(':', 1)[1]}"
    media = pending.pop(key, None)
    if media is None:
        await cb.answer("Файл уже недоступен, отправьте его заново.", show_alert=True)
        return
    try:
        await publish(media)
        await cb.message.edit_text("✅ Отправлено в группу (повторно).")
    except Exception as e:
        logging.exception("Ошибка при повторной отправке")
        await cb.message.edit_text(f"❌ Не удалось отправить: {escape(str(e))}")
    await cb.answer()


@group.message(F.audio | AUDIO_DOC)
async def on_group_media(message: Message):
    # Свои посты и сообщения других ботов не проверяем
    if message.from_user is None or message.from_user.is_bot:
        return
    media = extract_media(message)
    if media is None:
        return

    first_id = find_duplicate(media)
    if first_id is None:
        save_track(media, message.message_id)  # запоминаем новый трек
        return

    text = (
        f"⚠️ {message.from_user.mention_html()}, такой трек уже публиковали — "
        f"{where_html(first_id)}."
    )
    deleted = False
    if DELETE_DUPLICATES:
        try:
            await message.delete()
            deleted = True
        except Exception:
            logging.warning("Не удалось удалить дубликат (нужны права админа)")

    if deleted:
        await bot.send_message(GROUP_ID, text, link_preview_options=NO_PREVIEW)
    else:
        await message.reply(text, link_preview_options=NO_PREVIEW)


dp.include_routers(private, group)


# ---------- HTTP-сервер для Render ----------
async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def start_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host="0.0.0.0", port=PORT).start()
    logging.info("HTTP-сервер запущен на порту %s", PORT)
    return runner


async def main():
    runner = await start_web_server()
    try:
        logging.info("Версия: дубликаты + новая подпись")  # видно в логах, что код новый
        logging.info("Администраторы: %s", sorted(ADMIN_IDS))
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())

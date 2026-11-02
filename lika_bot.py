import asyncio
import logging
import os
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message

# ---------- Настройки из переменных окружения ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = os.getenv("GROUP_ID")          # например: -1001234567890
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS")    # например: 111111111,222222222

LINK_TEXT = os.getenv("LINK_TEXT", "Музыка ИИ")
LINK_URL = os.getenv("LINK_URL", "https://t.me/your_channel")

if not BOT_TOKEN or not GROUP_ID or not ADMIN_IDS_RAW:
    raise RuntimeError(
        "Не заданы переменные окружения: BOT_TOKEN, GROUP_ID, ADMIN_IDS"
    )

GROUP_ID = int(GROUP_ID)

# Разбираем список ID: допускаются запятые, пробелы и точки с запятой
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

# Хендлеры срабатывают только в личке и только для ID из списка.
# Остальные сообщения не обрабатываются и остаются без ответа.
dp.message.filter(
    F.chat.type == "private",
    F.from_user.id.in_(ADMIN_IDS),
)


def build_caption() -> str:
    """Подпись со скрытой гиперссылкой (HTML)."""
    return f'🎵 <a href="{escape(LINK_URL, quote=True)}">{escape(LINK_TEXT)}</a>'


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer("Привет! Отправь мне аудиофайл, и я перешлю его в группу.")


@dp.message(F.audio)
async def handle_audio(message: Message):
    try:
        await bot.send_audio(
            chat_id=GROUP_ID,
            audio=message.audio.file_id,
            caption=build_caption(),
            title=message.audio.title,
            performer=message.audio.performer,
            duration=message.audio.duration,
        )
        await message.answer("✅ Отправлено в группу.")
    except Exception as e:
        logging.exception("Ошибка при отправке аудио")
        await message.answer(f"❌ Не удалось отправить: {e}")


@dp.message(F.document & F.document.mime_type.startswith("audio/"))
async def handle_audio_document(message: Message):
    """Если музыка пришла как файл (документ), а не как аудио."""
    try:
        await bot.send_document(
            chat_id=GROUP_ID,
            document=message.document.file_id,
            caption=build_caption(),
        )
        await message.answer("✅ Отправлено в группу.")
    except Exception as e:
        logging.exception("Ошибка при отправке документа")
        await message.answer(f"❌ Не удалось отправить: {e}")


@dp.message(F.voice)
async def handle_voice(message: Message):
    try:
        await bot.send_voice(
            chat_id=GROUP_ID,
            voice=message.voice.file_id,
            caption=build_caption(),
        )
        await message.answer("✅ Отправлено в группу.")
    except Exception as e:
        logging.exception("Ошибка при отправке голосового")
        await message.answer(f"❌ Не удалось отправить: {e}")


async def main():
    logging.info("Разрешённые администраторы: %s", sorted(ADMIN_IDS))
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
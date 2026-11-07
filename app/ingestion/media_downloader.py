import asyncio
from pathlib import Path

from telethon import TelegramClient

from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import TelegramMessage, TelegramMedia


client = TelegramClient(
    "data/sireader",
    settings.TELEGRAM_API_ID,
    settings.TELEGRAM_API_HASH,
)

MEDIA_DIR = Path("data/media")
MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def detect_media_type(message):
    if message.photo:
        return "photo"

    if message.video:
        return "video"

    if message.audio:
        return "audio"

    if message.document:
        mime = message.document.mime_type or ""

        if mime.startswith("image/"):
            return "photo"

        if mime.startswith("video/"):
            return "video"

        if mime.startswith("audio/"):
            return "audio"

        return "document"

    return None


async def main():
    await client.start()

    chat_id = settings.TELEGRAM_TARGET_CHAT_ID

    downloaded = 0
    skipped = 0

    print("MEMERIKSA MEDIA TELEGRAM...")

    async for message in client.iter_messages(chat_id, limit=100):
        media_type = detect_media_type(message)

        if not media_type:
            continue

        with SessionLocal() as db:
            telegram_message = (
                db.query(TelegramMessage)
                .filter_by(
                    chat_id=chat_id,
                    telegram_message_id=message.id,
                )
                .first()
            )

            if not telegram_message:
                skipped += 1
                continue

            existing = (
                db.query(TelegramMedia)
                .filter_by(message_id=telegram_message.id)
                .first()
            )

            if existing:
                skipped += 1
                continue

            file_path = await message.download_media(
                file=str(MEDIA_DIR)
            )

            if not file_path:
                skipped += 1
                continue

            file_path = Path(file_path)

            mime_type = None
            file_size = None

            if message.document:
                mime_type = message.document.mime_type
                file_size = message.document.size

            elif message.photo:
                file_size = file_path.stat().st_size

            elif message.video:
                mime_type = "video/mp4"
                file_size = file_path.stat().st_size

            elif message.audio:
                mime_type = "audio/unknown"
                file_size = file_path.stat().st_size

            db.add(
                TelegramMedia(
                    message_id=telegram_message.id,
                    media_type=media_type,
                    file_path=str(file_path),
                    mime_type=mime_type,
                    file_size=file_size,
                    original_filename=file_path.name,
                )
            )

            db.commit()
            downloaded += 1

            print(
                f"[TERSIMPAN] {media_type}: "
                f"{file_path.name}"
            )

    print("\nSELESAI!")
    print("Berhasil diunduh:", downloaded)
    print("Dilewati:", skipped)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
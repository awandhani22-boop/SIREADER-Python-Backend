from pathlib import Path

from app.database.connection import SessionLocal
from app.database.models import TelegramMessage, TelegramMedia


MEDIA_DIR = Path("data/media")
MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def detect_media_type(message):
    if message.sticker:
        return "sticker"

    if message.voice:
        return "voice"

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


async def download_message_media(message, chat_id):
    media_type = detect_media_type(message)

    if not media_type:
        return False

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
            print("[MEDIA] Pesan belum ada di database.")
            return False

        message_db_id = telegram_message.id

        existing = (
            db.query(TelegramMedia)
            .filter_by(message_id=message_db_id)
            .first()
        )

        if existing:
            return False

    file_path = await message.download_media(
        file=str(MEDIA_DIR)
    )

    if not file_path:
        print("[MEDIA] Gagal mengunduh:", message.id)
        return False

    file_path = Path(file_path)

    if not file_path.exists():
        return False

    file_info = message.file
    mime_type = file_info.mime_type if file_info else None

    if message.photo:
        mime_type = "image/jpeg"
    elif message.voice:
        mime_type = "audio/ogg"

    with SessionLocal() as db:
        existing = (
            db.query(TelegramMedia)
            .filter_by(message_id=message_db_id)
            .first()
        )

        if existing:
            return False

        db.add(
            TelegramMedia(
                message_id=message_db_id,
                media_type=media_type,
                file_path=str(file_path),
                mime_type=mime_type,
                file_size=file_path.stat().st_size,
                original_filename=file_path.name,
            )
        )

        db.commit()

    print(
        f"[MEDIA TERSIMPAN] {media_type}: "
        f"{file_path.name}"
    )

    return True
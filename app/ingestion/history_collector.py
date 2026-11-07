
import asyncio
from datetime import timezone

from telethon import TelegramClient
from sqlalchemy import select

from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import TelegramMessage


client = TelegramClient(
    "data/sireader",
    settings.TELEGRAM_API_ID,
    settings.TELEGRAM_API_HASH,
)


def get_telegram_date(message):
    """Mengambil waktu asli pesan Telegram dalam UTC."""

    telegram_date = message.date

    if telegram_date is None:
        return None

    if telegram_date.tzinfo is None:
        telegram_date = telegram_date.replace(
            tzinfo=timezone.utc
        )
    else:
        telegram_date = telegram_date.astimezone(
            timezone.utc
        )

    return telegram_date


def save_message(message, chat_id):
    """Menyimpan pesan teks saja."""

    message_text = message.message or ""

    # Abaikan foto, video, stiker, dan media tanpa teks.
    if not message_text.strip():
        return False

    with SessionLocal() as db:
        existing = db.scalar(
            select(TelegramMessage).where(
                TelegramMessage.chat_id == chat_id,
                TelegramMessage.telegram_message_id == message.id,
            )
        )

        telegram_date = get_telegram_date(message)

        if existing:
            if existing.telegram_date is None:
                existing.telegram_date = telegram_date
                db.commit()

            return False

        sender = (
            str(message.sender_id)
            if message.sender_id is not None
            else None
        )

        reply_to_id = (
            message.reply_to.reply_to_msg_id
            if message.reply_to
            else None
        )

        db.add(
            TelegramMessage(
                chat_id=chat_id,
                telegram_message_id=message.id,
                sender=sender,
                message_text=message_text,
                reply_to_id=reply_to_id,
                is_edited=False,
                telegram_date=telegram_date,
            )
        )

        db.commit()
        return True


async def main():
    await client.start()

    chat_id = settings.TELEGRAM_TARGET_CHAT_ID

    print("MENGAMBIL RIWAYAT TEKS MENTOR...")
    print("Grup ID:", chat_id)
    print("Mode: TEXT ONLY")

    total = 0
    ignored = 0

    async for message in client.iter_messages(
        chat_id,
        limit=100,
    ):
        if save_message(message, chat_id):
            total += 1

            if total % 10 == 0:
                print("Pesan teks baru disimpan:", total)

        elif not (message.message or "").strip():
            ignored += 1

    print("\nSELESAI!")
    print("Pesan teks baru:", total)
    print("Pesan tanpa teks diabaikan:", ignored)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

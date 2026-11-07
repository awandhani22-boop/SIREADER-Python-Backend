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


async def main():
    chat_id = settings.TELEGRAM_TARGET_CHAT_ID

    # Ambil ID pesan yang belum memiliki waktu asli Telegram.
    with SessionLocal() as db:
        message_ids = db.scalars(
            select(TelegramMessage.telegram_message_id)
            .where(
                TelegramMessage.chat_id == chat_id,
                TelegramMessage.telegram_date.is_(None),
            )
            .order_by(TelegramMessage.telegram_message_id.asc())
        ).all()

    total = len(message_ids)

    print("TOTAL PESAN YANG PERLU DIPERBAIKI:", total)

    if total == 0:
        print("Tidak ada pesan yang perlu diperbaiki.")
        return

    await client.start()

    updated = 0
    missing = 0
    batch_size = 50

    for start in range(0, total, batch_size):
        batch_ids = message_ids[start:start + batch_size]

        telegram_messages = await client.get_messages(
            chat_id,
            ids=batch_ids,
        )

        if not isinstance(telegram_messages, list):
            telegram_messages = [telegram_messages]

        telegram_by_id = {
            message.id: message
            for message in telegram_messages
            if message is not None
        }

        with SessionLocal() as db:
            saved_messages = db.scalars(
                select(TelegramMessage).where(
                    TelegramMessage.chat_id == chat_id,
                    TelegramMessage.telegram_message_id.in_(batch_ids),
                )
            ).all()

            saved_by_id = {
                message.telegram_message_id: message
                for message in saved_messages
            }

            for message_id in batch_ids:
                telegram_message = telegram_by_id.get(message_id)
                saved_message = saved_by_id.get(message_id)

                if not telegram_message or not saved_message:
                    missing += 1
                    continue

                telegram_date = telegram_message.date

                if telegram_date.tzinfo is None:
                    telegram_date = telegram_date.replace(
                        tzinfo=timezone.utc
                    )
                else:
                    telegram_date = telegram_date.astimezone(timezone.utc)

                saved_message.telegram_date = telegram_date
                updated += 1

            db.commit()

        print(
            f"Progres: {min(start + batch_size, total)}/{total} "
            f"| Berhasil: {updated} | Tidak ditemukan: {missing}"
        )

    await client.disconnect()

    print("\nBACKFILL SELESAI")
    print("Total diperbarui:", updated)
    print("Tidak ditemukan:", missing)


if __name__ == "__main__":
    asyncio.run(main())

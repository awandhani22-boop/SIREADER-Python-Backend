import asyncio
from telethon import TelegramClient
from sqlalchemy import select

from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import TelegramMessage


async def main():
    client = TelegramClient(
        "data/sireader",
        settings.TELEGRAM_API_ID,
        settings.TELEGRAM_API_HASH,
    )

    await client.start()

    chat_id = settings.TELEGRAM_TARGET_CHAT_ID

    for message_id in [20496, 20506, 20515, 20520]:
        message = await client.get_messages(
            chat_id,
            ids=message_id,
        )

        with SessionLocal() as db:
            saved = db.scalar(
                select(TelegramMessage).where(
                    TelegramMessage.chat_id == chat_id,
                    TelegramMessage.telegram_message_id == message_id,
                )
            )

        print(f"\n--- PESAN {message_id} ---")
        print(
            "Waktu asli Telegram:",
            message.date if message else None,
        )
        print(
            "Waktu database:",
            saved.received_at if saved else None,
        )

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

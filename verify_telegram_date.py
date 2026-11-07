from sqlalchemy import select

from app.database.connection import SessionLocal
from app.database.models import TelegramMessage

chat_id = -1003014486607
message_ids = [20496, 20506, 20515, 20520]

with SessionLocal() as db:
    for message_id in message_ids:
        message = db.scalar(
            select(TelegramMessage).where(
                TelegramMessage.chat_id == chat_id,
                TelegramMessage.telegram_message_id == message_id,
            )
        )

        print(f"\n--- PESAN {message_id} ---")

        if message:
            print("Waktu asli Telegram:", message.telegram_date)
            print("Waktu diterima SIREADER:", message.received_at)
        else:
            print("Pesan tidak ditemukan")

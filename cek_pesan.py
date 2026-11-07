from sqlalchemy import select

from app.database.connection import SessionLocal
from app.database.models import TelegramMessage

with SessionLocal() as db:
    rows = db.scalars(
        select(TelegramMessage)
        .order_by(TelegramMessage.telegram_message_id.desc())
        .limit(5)
    ).all()

    for x in rows:
        print("-------------------------")
        print("ID:", x.telegram_message_id)
        print("Reply:", x.reply_to_id)
        print("Edited:", x.is_edited)
        print("Text:", (x.message_text or "")[:150])
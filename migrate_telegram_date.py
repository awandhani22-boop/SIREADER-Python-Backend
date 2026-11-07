from sqlalchemy import text
from app.database.connection import SessionLocal

with SessionLocal() as db:
    db.execute(text("""
        ALTER TABLE telegram_messages
        ADD COLUMN IF NOT EXISTS telegram_date TIMESTAMPTZ
    """))

    db.execute(text("""
        CREATE INDEX IF NOT EXISTS ix_telegram_messages_telegram_date
        ON telegram_messages (telegram_date)
    """))

    db.commit()

print("Kolom telegram_date berhasil disiapkan.")

from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import TelegramMessage

with SessionLocal() as db:
    test_message = (
        db.query(TelegramMessage)
        .filter(
            TelegramMessage.chat_id == settings.TELEGRAM_TARGET_CHAT_ID,
            TelegramMessage.telegram_message_id == 999999999,
        )
        .first()
    )

    if test_message:
        db.delete(test_message)
        db.commit()
        print("PESAN TEST BERHASIL DIHAPUS!")
    else:
        print("PESAN TEST TIDAK DITEMUKAN.")
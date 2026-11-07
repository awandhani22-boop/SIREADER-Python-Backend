from app.config import settings
from app.ingestion.context_reader import get_reply_context

reply = get_reply_context(
    chat_id=settings.TELEGRAM_TARGET_CHAT_ID,
    telegram_message_id=20515,
)

if reply:
    print("REPLY CONTEXT BERHASIL!")
    print("ID Pesan Asli:", reply["telegram_message_id"])
    print("Isi Pesan Asli:", reply["text"])
else:
    print("PESAN ASLI TIDAK DITEMUKAN")
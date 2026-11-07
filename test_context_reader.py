from app.config import settings
from app.ingestion.context_reader import get_message_context

context = get_message_context(
    chat_id=settings.TELEGRAM_TARGET_CHAT_ID,
    telegram_message_id=20519,
    before=5,
    after=5,
)

if context is None:
    print("PESAN TARGET TIDAK DITEMUKAN")
else:
    print("CONTEXT ENGINE BERHASIL!")

    for message in context:
        print("-------------------------")
        print("ID:", message["telegram_message_id"])
        print("Reply:", message["reply_to_id"])
        print("Text:", message["text"])
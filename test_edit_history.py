from app.config import settings
from app.ingestion.context_reader import get_message_edit_history

result = get_message_edit_history(
    chat_id=settings.TELEGRAM_TARGET_CHAT_ID,
    telegram_message_id=20519,
)

if result is None:
    print("PESAN TIDAK DITEMUKAN")
else:
    print("EDIT HISTORY READER BERHASIL!")
    print("ID:", result["telegram_message_id"])
    print("Teks terbaru:", result["current_text"])
    print("Pernah diedit:", result["is_edited"])

    print("\nRIWAYAT PERUBAHAN:")

    if not result["edit_history"]:
        print("Belum ada riwayat edit.")
    else:
        for index, edit in enumerate(result["edit_history"], start=1):
            print("-------------------------")
            print("Versi:", index)
            print("Teks lama:", edit["old_text"])
            print("Teks baru:", edit["new_text"])
            print("Waktu:", edit["edited_at"])
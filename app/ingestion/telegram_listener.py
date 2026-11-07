
import asyncio
from datetime import timezone

from telethon import TelegramClient, events
from sqlalchemy import select

from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import (
    TelegramMessage,
    TelegramMessageEdit,
)
from app.ai.pipeline import process_telegram_message
from app.ingestion.debounce import MessageDebouncer


client = TelegramClient(
    "data/sireader",
    settings.TELEGRAM_API_ID,
    settings.TELEGRAM_API_HASH,
)


def get_telegram_date(message):
    """Mengambil waktu asli Telegram dalam UTC."""

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


def save_message(message, chat_id, is_edited=False):
    """Menyimpan pesan teks saja."""

    message_text = message.message or ""

    if not message_text.strip():
        return False

    with SessionLocal() as db:
        existing = db.scalar(
            select(TelegramMessage).where(
                TelegramMessage.chat_id == chat_id,
                TelegramMessage.telegram_message_id == message.id,
            )
        )

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

        telegram_date = get_telegram_date(message)

        if existing:
            existing.sender = sender
            existing.message_text = message_text
            existing.reply_to_id = reply_to_id

            if existing.telegram_date is None:
                existing.telegram_date = telegram_date

            existing.is_edited = (
                existing.is_edited or is_edited
            )

        else:
            db.add(
                TelegramMessage(
                    chat_id=chat_id,
                    telegram_message_id=message.id,
                    sender=sender,
                    message_text=message_text,
                    reply_to_id=reply_to_id,
                    is_edited=is_edited,
                    telegram_date=telegram_date,
                )
            )

        db.commit()

    return True


def report_pipeline_result(chat_id, message_id, result):
    """Menampilkan ringkasan hasil Pipeline."""

    status = (
        result.get("status")
        if isinstance(result, dict)
        else "INVALID_RESULT"
    )

    print(
        f"[AI PIPELINE] Chat: {chat_id} | "
        f"Message: {message_id} | Status: {status}"
    )

    if status == "ANALYZED":
        analysis = result.get("analysis") or {}
        final_analysis = analysis.get("final_analysis") or {}

        print(
            "[AI SUMMARY]",
            final_analysis.get("summary", "Tidak ada ringkasan."),
        )

    elif status == "FAILED":
        print(
            "[AI ERROR]",
            result.get("error", "Kesalahan tidak diketahui."),
        )


debouncer = MessageDebouncer(
    delay_seconds=settings.AI_DEBOUNCE_SECONDS,
    processor=process_telegram_message,
    on_result=report_pipeline_result,
)


@client.on(events.NewMessage(chats=settings.TELEGRAM_TARGET_CHAT_ID))
async def handle_new_message(event):
    message_text = event.message.message or ""

    # Abaikan semua pesan tanpa teks.
    if not message_text.strip():
        print(
            f"[PESAN DIABAIKAN] ID: {event.message.id} | "
            "Tidak memiliki teks."
        )
        return

    saved = save_message(
        event.message,
        event.chat_id,
    )

    if not saved:
        return

    print(
        f"[PESAN TEKS BARU] ID: {event.message.id} | "
        f"{message_text}"
    )

    # Pipeline belum berjalan sampai diaktifkan secara eksplisit.
    if not settings.AI_PIPELINE_ENABLED:
        print(
            "[PIPELINE NONAKTIF] Pesan tersimpan, "
            "belum dikirim ke AI."
        )
        return

    # Pesan baru mereset timer debounce untuk chat tersebut.
    debouncer.schedule(
        event.chat_id,
        event.message.id,
    )

    print(
        f"[DEBOUNCE] Menunggu {settings.AI_DEBOUNCE_SECONDS} "
        "detik tanpa pesan baru."
    )


@client.on(events.MessageEdited(chats=settings.TELEGRAM_TARGET_CHAT_ID))
async def handle_edited_message(event):
    with SessionLocal() as db:
        existing = db.scalar(
            select(TelegramMessage).where(
                TelegramMessage.chat_id == event.chat_id,
                TelegramMessage.telegram_message_id == event.message.id,
            )
        )

        new_text = event.message.message or ""

        if existing:
            old_text = existing.message_text or ""
            updated = False

            if existing.telegram_date is None:
                existing.telegram_date = get_telegram_date(
                    event.message
                )
                updated = True

            if old_text != new_text:
                db.add(
                    TelegramMessageEdit(
                        message_id=existing.id,
                        old_text=old_text,
                        new_text=new_text,
                    )
                )

                existing.message_text = new_text
                existing.is_edited = True
                updated = True

            if updated:
                db.commit()

                print(
                    f"[PESAN TEKS DIEDIT] ID: {event.message.id}"
                )

                print(
                    "[INFO] Reanalisis pesan edit belum diaktifkan."
                )

        else:
            if new_text.strip():
                save_message(
                    event.message,
                    event.chat_id,
                    is_edited=True,
                )
            else:
                print(
                    f"[EDIT DIABAIKAN] ID: {event.message.id} | "
                    "Tidak memiliki teks."
                )


async def main():
    await client.start()

    print("SIREADER TEXT LISTENER AKTIF!")
    print("Grup:", settings.TELEGRAM_TARGET_CHAT_ID)
    print("Mode: TEXT ONLY")
    print("Debounce:", settings.AI_DEBOUNCE_SECONDS, "detik")
    print("Pipeline aktif:", settings.AI_PIPELINE_ENABLED)
    print("Trading mode:", settings.TRADING_MODE)
    print("Manual approval:", settings.REQUIRE_MANUAL_APPROVAL)
    print("Menunggu pesan Telegram...")

    try:
        await client.run_until_disconnected()

    finally:
        await debouncer.close()

        if client.is_connected():
            await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

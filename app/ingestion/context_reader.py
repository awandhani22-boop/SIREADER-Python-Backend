
from sqlalchemy import select, func, exists, or_

from app.database.models import (
    TelegramMessage,
    TelegramMessageEdit,
)
from app.database.connection import SessionLocal


def text_message_condition():
    """
    Pesan dianggap memiliki konten teks jika:
    - Teks aktif tidak kosong; atau
    - Riwayat edit memiliki teks.
    """

    edit_has_text = exists(
        select(1).where(
            TelegramMessageEdit.message_id == TelegramMessage.id,
            or_(
                func.trim(
                    func.coalesce(
                        TelegramMessageEdit.old_text,
                        "",
                    )
                ) != "",
                func.trim(
                    func.coalesce(
                        TelegramMessageEdit.new_text,
                        "",
                    )
                ) != "",
            ),
        )
    )

    current_text_exists = (
        func.trim(
            func.coalesce(
                TelegramMessage.message_text,
                "",
            )
        ) != ""
    )

    return or_(current_text_exists, edit_has_text)


def serialize_message(message):
    """Mengubah pesan database menjadi data teks."""

    return {
        "telegram_message_id": message.telegram_message_id,
        "text": message.message_text or "",
        "reply_to_id": message.reply_to_id,
        "sender": message.sender,
        "is_edited": message.is_edited,
    }


def get_message_context(
    chat_id,
    telegram_message_id,
    before=5,
    after=5,
):
    """Mengambil konteks pesan teks sebelum dan sesudah target."""

    with SessionLocal() as db:
        text_filter = text_message_condition()

        target = db.scalar(
            select(TelegramMessage).where(
                TelegramMessage.chat_id == chat_id,
                TelegramMessage.telegram_message_id
                == telegram_message_id,
                text_filter,
            )
        )

        if not target:
            return None

        previous_messages = db.scalars(
            select(TelegramMessage)
            .where(
                TelegramMessage.chat_id == chat_id,
                TelegramMessage.telegram_message_id
                < telegram_message_id,
                text_filter,
            )
            .order_by(
                TelegramMessage.telegram_message_id.desc()
            )
            .limit(before)
        ).all()

        next_messages = db.scalars(
            select(TelegramMessage)
            .where(
                TelegramMessage.chat_id == chat_id,
                TelegramMessage.telegram_message_id
                > telegram_message_id,
                text_filter,
            )
            .order_by(
                TelegramMessage.telegram_message_id.asc()
            )
            .limit(after)
        ).all()

        context = list(reversed(previous_messages))
        context.append(target)
        context.extend(next_messages)

        return [
            serialize_message(message)
            for message in context
        ]


def get_reply_context(chat_id, telegram_message_id):
    """Mengambil pesan induk dari reply jika memiliki teks."""

    with SessionLocal() as db:
        text_filter = text_message_condition()

        message = db.scalar(
            select(TelegramMessage).where(
                TelegramMessage.chat_id == chat_id,
                TelegramMessage.telegram_message_id
                == telegram_message_id,
                text_filter,
            )
        )

        if not message or not message.reply_to_id:
            return None

        original = db.scalar(
            select(TelegramMessage).where(
                TelegramMessage.chat_id == chat_id,
                TelegramMessage.telegram_message_id
                == message.reply_to_id,
                text_filter,
            )
        )

        if not original:
            return None

        return serialize_message(original)


def get_message_edit_history(chat_id, telegram_message_id):
    """Mengambil riwayat perubahan teks pesan."""

    with SessionLocal() as db:
        message = db.scalar(
            select(TelegramMessage).where(
                TelegramMessage.chat_id == chat_id,
                TelegramMessage.telegram_message_id
                == telegram_message_id,
            )
        )

        if not message:
            return None

        edits = db.scalars(
            select(TelegramMessageEdit)
            .where(
                TelegramMessageEdit.message_id == message.id
            )
            .order_by(
                TelegramMessageEdit.edited_at.asc()
            )
        ).all()

        return {
            "telegram_message_id": message.telegram_message_id,
            "current_text": message.message_text,
            "is_edited": message.is_edited,
            "edit_history": [
                {
                    "old_text": edit.old_text,
                    "new_text": edit.new_text,
                    "edited_at": (
                        edit.edited_at.isoformat()
                        if edit.edited_at
                        else None
                    ),
                }
                for edit in edits
            ],
        }

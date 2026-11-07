import asyncio
from telethon import TelegramClient
from app.config import settings

async def main():
    client = TelegramClient(
        "data/sireader",
        settings.TELEGRAM_API_ID,
        settings.TELEGRAM_API_HASH,
    )

    await client.start()

    chat_id = settings.TELEGRAM_TARGET_CHAT_ID

    for message_id in [20496, 20515]:
        message = await client.get_messages(chat_id, ids=message_id)

        print(f"\n--- PESAN {message_id} ---")

        if not message:
            print("Pesan tidak ditemukan")
            continue

        print("sender_id:", message.sender_id)
        print("from_id:", message.from_id)
        print("post_author:", message.post_author)
        print("peer_id:", message.peer_id)

        sender = await message.get_sender()

        if sender:
            print("sender_type:", type(sender).__name__)
            print("sender_id_entity:", getattr(sender, "id", None))
            print("sender_title:", getattr(sender, "title", None))
        else:
            print("sender_entity: None")

    await client.disconnect()

asyncio.run(main())

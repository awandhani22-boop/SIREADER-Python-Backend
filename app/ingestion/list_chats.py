import asyncio
from telethon import TelegramClient
from app.config import settings


async def main():
    client = TelegramClient(
        "data/sireader",
        settings.TELEGRAM_API_ID,
        settings.TELEGRAM_API_HASH,
    )

    await client.connect()

    if not await client.is_user_authorized():
        print("Session belum login.")
        return

    print("\nDAFTAR GRUP DAN CHANNEL:\n")

    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            print(f"Nama: {dialog.name}")
            print(f"ID  : {dialog.id}")
            print("-" * 40)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
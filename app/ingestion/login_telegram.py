from telethon import TelegramClient
from app.config import settings


client = TelegramClient(
    "data/sireader",
    settings.TELEGRAM_API_ID,
    settings.TELEGRAM_API_HASH,
)


async def main():
    await client.start()

    me = await client.get_me()

    print("\nTELEGRAM BERHASIL TERHUBUNG!")
    print("Nama akun:", me.first_name)
    print("User ID:", me.id)
    print("Session tersimpan secara lokal.")

    await client.disconnect()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
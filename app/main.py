
import asyncio
import logging

from app.approval import telegram_bot
from app.ingestion import telegram_listener
from app.database.connection import engine
from app.profiling import (
    setup_file_logging,
    install_sqlalchemy_events,
)


logger = logging.getLogger("SIREADER_MAIN")


async def run_sireader():
    """
    Entry point utama SIREADER.

    Menjalankan:
    1. Telegram Approval Bot
    2. Notification Outbox Worker
    3. Telegram Text Listener

    Semua berjalan pada satu event loop asyncio.
    """

    application = None

    bot_initialized = False
    bot_started = False
    updater_started = False
    post_init_completed = False
    listener_started = False

    listener_client = telegram_listener.client

    try:
        # ==================================================
        # INITIALIZATION
        # ==================================================

        setup_file_logging()
        install_sqlalchemy_events(engine)

        application = telegram_bot.build_application()

        logger.info("Menginisialisasi SIREADER...")

        await application.initialize()
        bot_initialized = True

        # run_polling biasanya menjalankan callback ini.
        # Karena kita menggunakan lifecycle manual, panggil sendiri.
        await telegram_bot.post_init(application)
        post_init_completed = True

        # ==================================================
        # START APPROVAL BOT
        # ==================================================

        await application.updater.start_polling()
        updater_started = True

        await application.start()
        bot_started = True

        logger.info("Telegram Approval Bot aktif.")

        # ==================================================
        # START TELEGRAM LISTENER
        # ==================================================

        await listener_client.start()
        listener_started = True

        logger.info("Telegram Listener aktif.")

        print()
        print("========================================")
        print("          SIREADER AKTIF")
        print("========================================")
        print("Approval Bot     : AKTIF")
        print("Notification     : AKTIF")
        print("Telegram Listener: AKTIF")
        print("Trading Mode     :", telegram_bot.settings.TRADING_MODE)
        print("AI Pipeline      :", telegram_bot.settings.AI_PIPELINE_ENABLED)
        print("Manual Approval  :", telegram_bot.settings.REQUIRE_MANUAL_APPROVAL)
        print("Auto Execution   : TERKUNCI")
        print("========================================")
        print("Tekan Ctrl+C untuk menghentikan SIREADER.")
        print()

        # Menunggu sampai Telegram Listener terputus
        # atau proses utama menerima Ctrl+C.
        await listener_client.run_until_disconnected()

    finally:
        logger.info("Memulai proses shutdown SIREADER...")

        # ==================================================
        # STOP TELEGRAM LISTENER
        # ==================================================

        try:
            await telegram_listener.debouncer.close()
        except Exception:
            logger.exception("Gagal menghentikan debounce.")

        if listener_client.is_connected():
            try:
                await listener_client.disconnect()
            except Exception:
                logger.exception("Gagal memutus Telegram Listener.")

        # ==================================================
        # STOP APPROVAL BOT
        # ==================================================

        if updater_started:
            try:
                await application.updater.stop()
            except Exception:
                logger.exception("Gagal menghentikan Telegram polling.")

        if bot_started:
            try:
                await application.stop()
            except Exception:
                logger.exception("Gagal menghentikan Telegram Bot.")

        # ==================================================
        # STOP NOTIFICATION WORKER
        # ==================================================

        if post_init_completed:
            try:
                await telegram_bot.post_stop(application)
            except Exception:
                logger.exception("Gagal menghentikan Notification Worker.")

        # ==================================================
        # SHUTDOWN APPLICATION
        # ==================================================

        if bot_initialized:
            try:
                await application.shutdown()
            except Exception:
                logger.exception("Gagal melakukan shutdown Telegram Bot.")

        logger.info("SIREADER berhasil melewati proses shutdown.")


def main():
    try:
        asyncio.run(run_sireader())

    except KeyboardInterrupt:
        print("\nSIREADER dihentikan oleh pengguna.")

    except Exception:
        logger.exception("SIREADER berhenti karena kesalahan fatal.")
        raise


if __name__ == "__main__":
    main()

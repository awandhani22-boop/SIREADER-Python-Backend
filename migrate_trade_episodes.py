from sqlalchemy import text

from app.database.connection import engine
from app.database.models import (
    Base,
    TradeEpisode,
    TradeEpisodeMessage,
)


def migrate():
    # Membuat tabel apabila belum tersedia.
    Base.metadata.create_all(
        bind=engine,
        tables=[
            TradeEpisode.__table__,
            TradeEpisodeMessage.__table__,
        ],
        checkfirst=True,
    )

    with engine.begin() as connection:

        # Pastikan tidak ada satu pesan yang sudah
        # terhubung ke beberapa episode.
        duplicates = connection.execute(
            text("""
                SELECT
                    telegram_message_db_id,
                    COUNT(*) AS total
                FROM trade_episode_messages
                GROUP BY telegram_message_db_id
                HAVING COUNT(*) > 1
            """)
        ).all()

        if duplicates:
            raise RuntimeError(
                "Migrasi dibatalkan: ditemukan pesan "
                "yang terhubung ke beberapa episode."
            )

        # Satu pesan Telegram hanya boleh dimiliki
        # satu episode.
        connection.execute(
            text("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                uq_trade_episode_message_single_owner
                ON trade_episode_messages (
                    telegram_message_db_id
                )
            """)
        )

    print("Tabel trade_episodes siap.")
    print("Tabel trade_episode_messages siap.")
    print("Unique index episode-message siap.")


if __name__ == "__main__":
    migrate()
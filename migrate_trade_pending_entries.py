from app.database.connection import engine
from app.database.models import Base, TradePendingEntry


def migrate():
    Base.metadata.create_all(
        bind=engine,
        tables=[TradePendingEntry.__table__],
        checkfirst=True,
    )

    print("Tabel trade_pending_entries berhasil disiapkan.")


if __name__ == "__main__":
    migrate()

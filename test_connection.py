from dotenv import load_dotenv
from sqlalchemy import create_engine, text
import os
import sys

load_dotenv(encoding="utf-8-sig")

database_url = os.getenv("DATABASE_URL")

if not database_url:
    print("ERROR: DATABASE_URL belum diisi di file .env")
    sys.exit(1)

# Gunakan driver psycopg 3
if database_url.startswith("postgresql://"):
    database_url = database_url.replace(
        "postgresql://",
        "postgresql+psycopg://",
        1
    )

try:
    engine = create_engine(
        database_url,
        connect_args={"sslmode": "require"},
        pool_pre_ping=True
    )

    with engine.connect() as connection:
        result = connection.execute(
            text("SELECT current_database(), current_user, now()")
        ).fetchone()

        print("KONEKSI SUPABASE BERHASIL!")
        print(f"Database : {result[0]}")
        print(f"User     : {result[1]}")
        print(f"Waktu DB : {result[2]}")

    engine.dispose()

except Exception as error:
    print("KONEKSI GAGAL.")
    print("Jenis error:", type(error).__name__)
    print("Detail:", str(error))
    sys.exit(1)
from sqlalchemy import inspect

inspector = inspect(engine)
print("Tabel tersedia:", inspector.get_table_names())

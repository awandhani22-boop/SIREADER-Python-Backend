import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

load_dotenv(encoding="utf-8-sig")

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL belum diatur di .env")

if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgresql://",
        "postgresql+psycopg://",
        1
    )

# Supabase Shared Pooler (Supavisor), session mode, port 5432.
# Dua proses Python (Telegram Bot dan Telegram Listener) masing-masing
# memiliki Engine sendiri. Maks koneksi per proses = pool_size + max_overflow
# = 3 + 2 = 5. Total dua proses = 10 koneksi ke Supavisor.
#
# pool_pre_ping=True dipertahankan: mendeteksi koneksi mati sebelum dipakai
# (biaya ~1 RTT per checkout, disengaja demi stabilitas approval/audit/outbox).
# pool_recycle membuang koneksi yang terlalu tua; fungsinya berbeda dari
# pre_ping dan bukan penggantinya.
engine = create_engine(
    DATABASE_URL,
    connect_args={"sslmode": "require"},
    pool_size=3,
    max_overflow=2,
    pool_timeout=10,
    pool_recycle=1800,
    pool_pre_ping=True,
    pool_use_lifo=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)
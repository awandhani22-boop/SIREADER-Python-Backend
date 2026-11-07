"""
Pengukuran latensi SIREADER — BEFORE patch.

Read-only. Tidak mengubah database, tidak approval, tidak kirim order MT5,
tidak kirim pesan Telegram ke grup manapun.

Memisahkan:
  - connection checkout time (waktu ambil koneksi dari pool)
  - query execution time (waktu query murni)
  - telegram getMe RTT (opsional, hanya jika token ada)

Jalankan dari C:\\SIREADER dengan venv aktif:
  python measure_latency_before.py
"""
from __future__ import annotations

import time
import statistics
from contextlib import contextmanager

from sqlalchemy import text

from app.database.connection import engine, SessionLocal
from app.approval.service import (
    list_pending_reviews,
    get_runtime_control,
)


def timed(fn, *args, **kwargs):
    t0 = time.perf_counter()
    result = fn(*args, **kwargs)
    dt = (time.perf_counter() - t0) * 1000.0
    return dt, result


def measure_checkout(n=5):
    """Waktu checkout koneksi dari pool, dipisah dari eksekusi query."""
    durations = []
    for _ in range(n):
        t0 = time.perf_counter()
        conn = engine.connect()
        checkout_ms = (time.perf_counter() - t0) * 1000.0

        t1 = time.perf_counter()
        conn.execute(text("SELECT 1"))
        query_ms = (time.perf_counter() - t1) * 1000.0

        conn.close()
        durations.append((checkout_ms, query_ms))
    return durations


def measure_fn(label, fn, n=3):
    samples = []
    for _ in range(n):
        dt, _ = timed(fn)
        samples.append(dt)
    print(f"{label:<28} n={n}  "
          f"min={min(samples):8.1f}ms  "
          f"med={statistics.median(samples):8.1f}ms  "
          f"max={max(samples):8.1f}ms")
    return samples


def main():
    print("=" * 60)
    print("SIREADER LATENCY — BEFORE PATCH")
    print("=" * 60)
    print("Mode: READ-ONLY. Tidak mengubah DB / approval / order MT5.")
    print()

    print("--- CONNECTION CHECKOUT vs QUERY (SELECT 1) ---")
    rows = measure_checkout(n=6)
    for i, (c, q) in enumerate(rows, 1):
        print(f"  attempt {i}: checkout={c:8.1f}ms  query(SELECT 1)={q:8.1f}ms")
    checkouts = [c for c, _ in rows]
    queries = [q for _, q in rows]
    print(f"  checkout  med={statistics.median(checkouts):8.1f}ms  "
          f"max={max(checkouts):8.1f}ms")
    print(f"  query     med={statistics.median(queries):8.1f}ms  "
          f"max={max(queries):8.1f}ms")
    print()

    print("--- SERVICE FUNCTIONS (round-trip nyata) ---")
    measure_fn("get_runtime_control (menu)", get_runtime_control, n=4)
    measure_fn("list_pending_reviews", list_pending_reviews, n=4)
    print()

    print("--- POOL STATUS ---")
    pool = engine.pool
    print(f"  class     : {pool.__class__.__name__}")
    for attr in ("size", "checkedin", "checkedout", "overflow"):
        fn = getattr(pool, attr, None)
        if callable(fn):
            try:
                print(f"  {attr:<10}: {fn()}")
            except Exception:
                pass
    print()
    print("Selesai. Tidak ada perubahan database.")


if __name__ == "__main__":
    main()

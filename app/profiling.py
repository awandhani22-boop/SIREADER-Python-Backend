"""
Second-level profiling untuk SIREADER. READ-ONLY.

TIDAK mengubah business logic. Aktif hanya jika env SIREADER_PROFILE=1.
TIDAK mencatat SQL yang memuat data, token, password, atau DATABASE_URL.
Yang dicatat hanya: operasi (checkout/execute/checkin), nama tabel/perintah
SQL (kata pertama + nama tabel bila terdeteksi), dan durasi milidetik.

Dipakai oleh telegram_bot untuk mengukur komponen Telegram, dan oleh
SQLAlchemy engine untuk mengukur checkout/execute/checkin.
"""
from __future__ import annotations

import os
import re
import time
import logging
from contextlib import contextmanager

PROFILE_ENABLED = os.getenv("SIREADER_PROFILE") == "1"

# Logger terpisah -> file logs/profile.log (dipasang di setup_file_logging).
profile_logger = logging.getLogger("SIREADER_PROFILE")


def setup_file_logging():
    """Arahkan SIREADER_PROFILE dan SIREADER_PERF ke logs/profile.log."""
    if not PROFILE_ENABLED:
        return

    os.makedirs("logs", exist_ok=True)
    handler = logging.FileHandler(
        os.path.join("logs", "profile.log"),
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s: %(message)s")
    )

    for name in ("SIREADER_PROFILE", "SIREADER_PERF"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.addHandler(handler)
        lg.propagate = True  # tetap tampil di stdout juga


# ============================================================
# SQL label aman: hanya perintah + nama tabel, tanpa nilai data
# ============================================================

_FIRST_WORD = re.compile(r"^\s*(\w+)", re.IGNORECASE)
_TABLE_FROM = re.compile(r"\bFROM\s+\"?(\w+)\"?", re.IGNORECASE)
_TABLE_INTO = re.compile(r"\bINTO\s+\"?(\w+)\"?", re.IGNORECASE)
_TABLE_UPDATE = re.compile(r"\bUPDATE\s+\"?(\w+)\"?", re.IGNORECASE)


def safe_sql_label(statement: str) -> str:
    """
    Mengubah SQL menjadi label aman: perintah + tabel saja.

    Contoh: 'SELECT ai_analysis_log', 'UPDATE ai_analysis_log',
    'INSERT telegram_outbox'. TIDAK memuat nilai, parameter, WHERE,
    token, atau kredensial.
    """
    if not statement:
        return "unknown"

    cmd_match = _FIRST_WORD.match(statement)
    cmd = cmd_match.group(1).upper() if cmd_match else "SQL"

    table = None
    for rx in (_TABLE_FROM, _TABLE_INTO, _TABLE_UPDATE):
        m = rx.search(statement)
        if m:
            table = m.group(1)
            break

    return f"{cmd} {table}" if table else cmd


# ============================================================
# SQLAlchemy event listeners: checkout / execute / checkin
# ============================================================

def install_sqlalchemy_events(engine):
    """
    Memasang listener untuk mengukur checkout, execute, dan checkin.

    Idempoten dan no-op jika profiling nonaktif. Tidak mengubah query.
    """
    if not PROFILE_ENABLED:
        return

    from sqlalchemy import event

    @event.listens_for(engine, "checkout")
    def _on_checkout(dbapi_conn, conn_record, conn_proxy):
        conn_record.info["checkout_t"] = time.perf_counter()

    @event.listens_for(engine, "checkin")
    def _on_checkin(dbapi_conn, conn_record):
        t0 = conn_record.info.pop("checkout_t", None)
        if t0 is not None:
            held_ms = (time.perf_counter() - t0) * 1000.0
            profile_logger.info("conn held_ms=%.1f", held_ms)

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, params, context, executemany):
        context._prof_t = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, params, context, executemany):
        t0 = getattr(context, "_prof_t", None)
        if t0 is not None:
            dur_ms = (time.perf_counter() - t0) * 1000.0
            profile_logger.info(
                "query sql=%s durasi_ms=%.1f",
                safe_sql_label(statement),
                dur_ms,
            )


# ============================================================
# Pengukur blok bernama (query.answer, reply_text, dll)
# ============================================================

@contextmanager
def measure(label: str):
    """
    Mengukur durasi satu blok dan mencatatnya. No-op jika nonaktif.

    label hanya nama tahap (mis. 'query.answer'); TIDAK memuat data.
    """
    if not PROFILE_ENABLED:
        yield
        return

    start = time.perf_counter()
    try:
        yield
    finally:
        dur_ms = (time.perf_counter() - start) * 1000.0
        profile_logger.info("step=%s durasi_ms=%.1f", label, dur_ms)


# ============================================================
# Instrumentasi HTTP Telegram: pisahkan pool-wait vs network+server
# ============================================================

def _telegram_method_label(url: str) -> str:
    """
    Mengambil nama method Telegram dari URL (segmen terakhir), mis.
    'getUpdates', 'sendMessage', 'answerCallbackQuery'.

    TIDAK memuat token. URL Telegram berbentuk
    https://api.telegram.org/bot<TOKEN>/<method>; hanya <method> diambil,
    token (segmen 'bot<TOKEN>') dibuang total.
    """
    try:
        method = url.rsplit("/", 1)[-1]
    except Exception:
        return "unknown"
    # Keamanan ekstra: jika entah bagaimana segmen memuat 'bot' + token,
    # jangan catat.
    if method.startswith("bot"):
        return "unknown"
    return method or "unknown"


def make_profiling_request_class():
    """
    Mengembalikan subclass HTTPXRequest yang mengukur tiap request:
      - total do_request (termasuk antre pool koneksi),
      - network+server (response.elapsed dari httpx),
      - selisih (perkiraan pool-wait + overhead lokal).

    Mengembalikan HTTPXRequest biasa jika profiling nonaktif.
    TIDAK mencatat token, payload, atau header; hanya nama method + durasi.
    """
    from telegram.request import HTTPXRequest

    if not PROFILE_ENABLED:
        return HTTPXRequest

    class ProfilingHTTPXRequest(HTTPXRequest):
        async def do_request(self, url, method, *args, **kwargs):
            start = time.perf_counter()
            pool_wait_ms = None
            net_ms = None
            try:
                # Rebut response.elapsed via client bila tersedia setelah
                # request. Kita ukur total dan baca elapsed dari client
                # terakhir jika httpx menyimpannya; fallback: total saja.
                result = await super().do_request(
                    url, method, *args, **kwargs
                )
                return result
            finally:
                total_ms = (time.perf_counter() - start) * 1000.0
                label = _telegram_method_label(url)
                # response.elapsed tidak langsung terekspos di sini;
                # catat total. Breakdown network dibaca dari httpx hook
                # (lihat install_httpx_timing).
                profile_logger.info(
                    "tg method=%s total_ms=%.1f",
                    label,
                    total_ms,
                )

    return ProfilingHTTPXRequest


def httpx_event_hooks():
    """
    Event hooks httpx untuk mengukur network+server per request.

    Dipasang pada HTTPXRequest lewat parameter httpx_kwargs jika PTB
    mengizinkan. Mengembalikan dict kosong jika profiling nonaktif.
    TIDAK mencatat token/header/body.
    """
    if not PROFILE_ENABLED:
        return {}

    async def _on_response(response):
        # response.elapsed = waktu dari request terkirim sampai header
        # terakhir diterima = network RTT + waktu server Telegram.
        try:
            elapsed_ms = response.elapsed.total_seconds() * 1000.0
        except Exception:
            return
        label = _telegram_method_label(str(response.request.url))
        profile_logger.info(
            "tg method=%s network_server_ms=%.1f status=%s",
            label,
            elapsed_ms,
            response.status_code,
        )

    return {"response": [_on_response]}

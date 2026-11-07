#!/usr/bin/env python
"""Pemeriksaan READ-ONLY kesiapan provider Anthropic dan key SIREADER_CLAUDE di OmniRoute.

Tidak mengubah database/konfigurasi, tidak memanggil AI, tidak mencetak credential.
Snapshot konsisten dibuat dengan SQLite backup API ke file sementara, lalu dihapus
di blok finally. Kolom `key`, `api_key`, `access_token`, dan sejenisnya tidak pernah
dibaca nilainya: hanya dicek kosong/tidak lewat ekspresi SQL.

Contoh:
    python check_omniroute_sireader_key.py --provider-prefix anthropic
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlparse

KEY_NAME_DEFAULT = "SIREADER_CLAUDE"
MODEL_IDS = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")
CONN_OK_STATUS = {"active", "ok", "success", "connected", "valid", "healthy"}
PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def default_db_path():
    base = os.environ.get("DATA_DIR") or os.path.join(os.path.expanduser("~"), ".omniroute")
    return os.path.join(base, "storage.sqlite")


def snapshot(src_path):
    """Salin DB ke file sementara via backup API. Sumber dibuka read-only."""
    if not os.path.isfile(src_path):
        raise FileNotFoundError(f"Database tidak ditemukan: {src_path}")
    fd, tmp_path = tempfile.mkstemp(prefix="or_check_", suffix=".sqlite")
    os.close(fd)
    src_uri = "file:" + src_path.replace("\\", "/") + "?mode=ro"
    src = dst = None
    try:
        src = sqlite3.connect(src_uri, uri=True, timeout=10)
        dst = sqlite3.connect(tmp_path)
        src.backup(dst)
    except Exception:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()
        cleanup(tmp_path)
        raise
    src.close()
    dst.close()
    return tmp_path


def cleanup(path):
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[peringatan] gagal hapus {path + suffix}: {exc}", file=sys.stderr)


def table_cols(conn, table):
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def has_table(conn, table):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def host_of(url):
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def nonempty_expr(cols, names):
    """Ekspresi SQL boolean 'kolom ada isinya' tanpa membaca nilainya."""
    parts = [f'("{n}" IS NOT NULL AND TRIM("{n}") != \'\')' for n in names if n in cols]
    return " OR ".join(parts) if parts else None


class Report:
    def __init__(self):
        self.rows = []

    def add(self, label, status, reason, source):
        self.rows.append((label, status, reason, source))

    def get(self, label):
        for r in self.rows:
            if r[0] == label:
                return r[1]
        return None


def check_provider(conn, prefix, rep, notes):
    src = "provider_nodes, provider_connections"
    if not has_table(conn, "provider_connections"):
        rep.add("Provider Anthropic", "UNKNOWN", "Tabel provider_connections tidak ada.", src)
        rep.add("Credential", "UNKNOWN", "Tabel provider_connections tidak ada.", src)
        return set()

    pc = table_cols(conn, "provider_connections")
    if "provider" not in pc:
        rep.add("Provider Anthropic", "UNKNOWN", "Kolom provider tidak ada.", src)
        rep.add("Credential", "UNKNOWN", "Kolom provider tidak ada.", src)
        return set()

    # Petakan prefix -> id provider. Node custom punya prefix; provider bawaan memakai id = prefix.
    ids = {prefix.lower()}
    node_info = []
    if has_table(conn, "provider_nodes"):
        nc = table_cols(conn, "provider_nodes")
        if "prefix" in nc and "id" in nc:
            sel = [c for c in ("id", "type", "name", "api_type", "base_url") if c in nc]
            q = f'SELECT {", ".join(chr(34) + c + chr(34) for c in sel)} FROM provider_nodes WHERE LOWER("prefix") = ?'
            for row in conn.execute(q, (prefix.lower(),)):
                d = dict(zip(sel, row))
                node_info.append(d)
                ids.add(str(d["id"]).lower())

    sel_cols = [c for c in ("id", "provider", "name", "auth_type", "is_active", "test_status", "error_code", "last_error_type") if c in pc]
    cred_expr = nonempty_expr(pc, ("api_key", "access_token"))
    cred_sel = f"({cred_expr})" if cred_expr else "NULL"
    select = ", ".join(f'"{c}"' for c in sel_cols) + f", {cred_sel} AS _has_cred"
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f'SELECT {select} FROM provider_connections WHERE LOWER("provider") IN ({placeholders})',
        tuple(ids),
    ).fetchall()
    names = sel_cols + ["_has_cred"]
    conns = [dict(zip(names, r)) for r in rows]

    if not conns:
        rep.add("Provider Anthropic", "NOT READY",
                f"Tidak ada koneksi untuk prefix '{prefix}' (id dicari: {sorted(ids)}).", src)
        rep.add("Credential", "MISSING", "Tidak ada koneksi provider, jadi tidak ada credential.", src)
        return ids

    # Apakah ini benar-benar Anthropic?
    def is_anthropic(c):
        pid = str(c.get("provider", "")).lower()
        if "anthropic" in pid or pid in ("claude",):
            return True
        for n in node_info:
            if str(n["id"]).lower() == pid:
                blob = " ".join(str(n.get(k, "")) for k in ("type", "api_type")).lower()
                if "anthropic" in blob or host_of(n.get("base_url", "")).endswith("anthropic.com"):
                    return True
        return False

    anth = [c for c in conns if is_anthropic(c)]
    for n in node_info:
        notes.append(f"Node prefix '{prefix}': type={n.get('type')}, api_type={n.get('api_type')}, host={host_of(n.get('base_url', '')) or '-'}")
    if not anth:
        hosts = sorted({host_of(n.get('base_url', '')) for n in node_info if n.get('base_url')})
        rep.add("Provider Anthropic", "NOT READY",
                f"Prefix '{prefix}' ada ({len(conns)} koneksi) tetapi bukan Anthropic. Host upstream: {hosts or 'tidak diketahui'}.", src)
        creds = [c["_has_cred"] for c in conns]
        rep.add("Credential", "UNKNOWN", "Credential milik provider non-Anthropic tidak dinilai: " + str(len(creds)) + " koneksi.", src)
        return ids

    # Credential
    if cred_expr is None:
        rep.add("Credential", "UNKNOWN", "Kolom api_key/access_token tidak ada di schema.", src)
        cred_present = False
    else:
        cred_present = any(c["_has_cred"] for c in anth)
        rep.add("Credential", "PRESENT" if cred_present else "MISSING",
                f"{sum(1 for c in anth if c['_has_cred'])} dari {len(anth)} koneksi Anthropic punya credential terisi (nilai tidak dibaca).",
                "provider_connections.api_key / access_token (hanya cek kosong)")

    # Status koneksi
    for c in anth:
        notes.append(
            "Koneksi '{n}': is_active={a}, test_status={t}, error_code={e}, last_error_type={l}".format(
                n=c.get("name"), a=c.get("is_active"), t=c.get("test_status"),
                e=c.get("error_code"), l=c.get("last_error_type"))
        )
    active = [c for c in anth if c.get("is_active") in (1, True)]
    if "test_status" not in pc:
        status, reason = "UNKNOWN", "Kolom test_status tidak ada; status koneksi belum dapat diverifikasi."
    elif not active:
        status, reason = "NOT READY", "Tidak ada koneksi Anthropic yang is_active=1."
    elif not cred_present:
        status, reason = "NOT READY", "Koneksi aktif tetapi credential kosong."
    else:
        ok = [c for c in active if c["_has_cred"] and str(c.get("test_status") or "").lower() in CONN_OK_STATUS]
        unknown = [c for c in active if not str(c.get("test_status") or "").strip()]
        if ok:
            status, reason = "READY", "Koneksi aktif, credential terisi, test_status OK (status tersimpan, bukan uji live)."
        elif unknown:
            status, reason = "UNKNOWN", "Koneksi aktif tetapi test_status kosong; belum pernah diuji."
        else:
            status, reason = "NOT READY", "test_status koneksi aktif bukan OK: " + ", ".join(sorted({str(c.get('test_status')) for c in active}))
    rep.add("Provider Anthropic", status, reason, "provider_connections.provider/is_active/test_status; provider_nodes.prefix/base_url")
    # Urutkan agar Provider tampil sebelum Credential.
    rep.rows.sort(key=lambda r: 0 if r[0] == "Provider Anthropic" else 1 if r[0] == "Credential" else 2)
    return ids


def parse_allowlist(raw):
    if raw is None:
        return None, "NULL"
    text = str(raw).strip()
    if text == "":
        return [], "string kosong"
    try:
        val = json.loads(text)
    except ValueError:
        return None, "bukan JSON valid"
    if not isinstance(val, list):
        return None, f"bukan list ({type(val).__name__})"
    return [str(v) for v in val], None


def check_key(conn, key_name, prefix, rep, notes):
    src = "api_keys"
    if not has_table(conn, "api_keys"):
        for lbl in ("Key SIREADER_CLAUDE", "Key status", "Model allowlist"):
            rep.add(lbl, "UNKNOWN", "Tabel api_keys tidak ada.", src)
        return None
    ac = table_cols(conn, "api_keys")
    needed = [c for c in ("id", "name") if c not in ac]
    if needed:
        for lbl in ("Key SIREADER_CLAUDE", "Key status", "Model allowlist"):
            rep.add(lbl, "UNKNOWN", f"Kolom wajib tidak ada: {needed}", src)
        return None

    # Kolom `key` sengaja TIDAK dipilih.
    sel = [c for c in ("id", "name", "allowed_models", "revoked_at", "expires_at", "last_used_at", "is_active") if c in ac]
    rows = conn.execute(
        f'SELECT {", ".join(chr(34) + c + chr(34) for c in sel)} FROM api_keys WHERE "name" = ?',
        (key_name,),
    ).fetchall()
    others = [r[0] for r in conn.execute('SELECT "name" FROM api_keys ORDER BY "name"')]
    if not rows:
        rep.add("Key SIREADER_CLAUDE", "NOT FOUND",
                f"Tidak ada key bernama '{key_name}'. Nama key yang ada: {others}. SIGNALREADER/SIREADER OMNI tidak dianggap pengganti.", src)
        rep.add("Key status", "UNKNOWN", "Key tidak ditemukan.", src)
        rep.add("Model allowlist", "FAIL", "Key tidak ditemukan, allowlist tidak dapat dibuktikan.", src)
        return None
    if len(rows) > 1:
        rep.add("Key SIREADER_CLAUDE", "FOUND", f"PERINGATAN: {len(rows)} key bernama '{key_name}'. Memakai yang pertama.", src)
    else:
        rep.add("Key SIREADER_CLAUDE", "FOUND", f"Satu key bernama '{key_name}'.", src + ".name")
    k = dict(zip(sel, rows[0]))

    # Status
    now = datetime.now(timezone.utc)
    reasons = []
    status = "ACTIVE"
    if "revoked_at" not in ac:
        status, reasons = "UNKNOWN", ["Kolom revoked_at tidak ada."]
    elif k.get("revoked_at"):
        status, reasons = "REVOKED", [f"revoked_at={k['revoked_at']}"]
    else:
        reasons.append("revoked_at kosong")
        exp = k.get("expires_at")
        if exp:
            try:
                dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt <= now:
                    status = "REVOKED"
                    reasons.append(f"sudah kedaluwarsa ({exp})")
                else:
                    reasons.append(f"expires_at={exp}")
            except ValueError:
                status = "UNKNOWN"
                reasons.append(f"expires_at tidak terbaca ({exp})")
        if "is_active" in k and k["is_active"] in (0, False):
            status = "REVOKED"
            reasons.append("is_active=0")
    rep.add("Key status", status, "; ".join(reasons), "api_keys.revoked_at/expires_at")

    # Allowlist
    expected = {f"{prefix}/{m}" for m in MODEL_IDS}
    if "allowed_models" not in ac:
        rep.add("Model allowlist", "FAIL", "Kolom allowed_models tidak ada; key tidak dapat dibuktikan terbatas.", src)
    else:
        lst, err = parse_allowlist(k.get("allowed_models"))
        if lst is None:
            rep.add("Model allowlist", "FAIL", f"allowed_models tidak terbaca: {err}.", "api_keys.allowed_models")
        elif not lst:
            rep.add("Model allowlist", "FAIL", "allowed_models kosong = UNRESTRICTED (semua model boleh).", "api_keys.allowed_models")
        else:
            got = set(lst)
            missing, extra = sorted(expected - got), sorted(got - expected)
            if not missing and not extra and len(lst) == len(got):
                rep.add("Model allowlist", "PASS", f"Tepat tiga model: {sorted(got)}.", "api_keys.allowed_models")
            else:
                parts = []
                if missing:
                    parts.append(f"kurang: {missing}")
                if extra:
                    parts.append(f"berlebih/salah: {extra}")
                if len(lst) != len(got):
                    parts.append("ada duplikat")
                rep.add("Model allowlist", "FAIL", "; ".join(parts), "api_keys.allowed_models")
    return k


def check_usage(conn, key_row, prefix, rep):
    label = "Riwayat penggunaan"
    if key_row is None:
        rep.add(label, "UNKNOWN", "Key tidak ditemukan, riwayat tidak dapat dikaitkan.", "-")
        return
    key_id = key_row["id"]
    allowed = {m for m in MODEL_IDS} | {f"{prefix}/{m}" for m in MODEL_IDS}
    sources = []
    total = 0
    models = {}
    latest = None
    linkable = False
    # Hubungan dibuktikan lewat kolom api_key_id (= api_keys.id) di tiap tabel log.
    for table, model_cols, ts_col in (
        ("call_logs", ("requested_model", "model"), "timestamp"),
        ("usage_history", ("model",), "timestamp"),
    ):
        if not has_table(conn, table):
            continue
        cols = table_cols(conn, table)
        if "api_key_id" not in cols:
            continue
        linkable = True
        used = [c for c in model_cols if c in cols]
        n = conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "api_key_id" = ?', (key_id,)).fetchone()[0]
        sources.append(f"{table}.api_key_id={n}")
        if table == "call_logs":
            total = n  # satu request = satu baris call_logs; usage_history menduplikasi
        elif not total:
            total = n
        for mc in used:
            for m, c in conn.execute(
                f'SELECT "{mc}", COUNT(*) FROM "{table}" WHERE "api_key_id" = ? GROUP BY 1', (key_id,)
            ):
                models.setdefault(str(m), 0)
                models[str(m)] = max(models[str(m)], c)
        if ts_col in cols and n:
            t = conn.execute(f'SELECT MAX("{ts_col}") FROM "{table}" WHERE "api_key_id" = ?', (key_id,)).fetchone()[0]
            latest = max(filter(None, [latest, t]), default=latest)

    if not linkable:
        rep.add(label, "UNKNOWN", "Tidak ada tabel log dengan kolom api_key_id; hubungan key-log tidak dapat dibuktikan.", "call_logs / usage_history")
        return
    src = "; ".join(sources)
    last_used = key_row.get("last_used_at")
    if total == 0:
        if last_used:
            rep.add(label, "WARNING",
                    f"Log kosong untuk key ini, tetapi api_keys.last_used_at={last_used}. Pemakaian tidak tercatat di log.", src + "; api_keys.last_used_at")
        else:
            rep.add(label, "PASS", "Key belum pernah dipakai (0 request di log, last_used_at kosong).", src)
        return
    outside = {m: c for m, c in models.items() if m not in allowed and m not in ("", "None")}
    desc = ", ".join(f"{m}={c}" for m, c in sorted(models.items()))
    if outside:
        rep.add(label, "WARNING",
                f"{total} request. Model di luar tiga yang diizinkan: {outside}. Semua model: {desc}.", src)
    elif last_used and latest and str(last_used) > str(latest):
        rep.add(label, "WARNING",
                f"{total} request, model: {desc}. last_used_at ({last_used}) lebih baru dari log terakhir ({latest}); sebagian pemakaian tidak tercatat.", src)
    else:
        rep.add(label, "PASS", f"{total} request, semua model dalam allowlist: {desc}.", src)


def main():
    ap = argparse.ArgumentParser(description="Cek read-only kesiapan Anthropic + key SIREADER_CLAUDE di OmniRoute.")
    ap.add_argument("--provider-prefix", required=True, help="Prefix provider di OmniRoute, mis. nama prefix untuk koneksi Anthropic.")
    ap.add_argument("--key-name", default=KEY_NAME_DEFAULT, help=f"Nama API key target (default {KEY_NAME_DEFAULT}).")
    ap.add_argument("--db", default=default_db_path(), help="Path storage.sqlite OmniRoute.")
    args = ap.parse_args()

    if not PREFIX_RE.match(args.provider_prefix):
        print("Prefix tidak valid (hanya huruf, angka, titik, garis bawah, strip).", file=sys.stderr)
        return 2

    rep, notes = Report(), []
    tmp = None
    try:
        tmp = snapshot(args.db)
        conn = sqlite3.connect(f"file:{tmp.replace(chr(92), '/')}?mode=ro", uri=True)
        try:
            check_provider(conn, args.provider_prefix, rep, notes)
            key_row = check_key(conn, args.key_name, args.provider_prefix, rep, notes)
            check_usage(conn, key_row, args.provider_prefix, rep)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - laporkan tanpa membocorkan isi data
        print(f"GAGAL membaca database: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        if tmp:
            cleanup(tmp)

    ok_map = {
        "Provider Anthropic": "READY",
        "Credential": "PRESENT",
        "Key SIREADER_CLAUDE": "FOUND",
        "Key status": "ACTIVE",
        "Model allowlist": "PASS",
        "Riwayat penggunaan": "PASS",
    }
    ready = all(rep.get(k) == v for k, v in ok_map.items())

    print("=" * 72)
    print("OmniRoute read-only check | key target:", args.key_name, "| prefix:", args.provider_prefix)
    print("Snapshot sementara dihapus setelah selesai. Tidak ada credential yang dibaca/dicetak.")
    print("=" * 72)
    for label, status, reason, source in rep.rows:
        print(f"{label}: {status}")
        print(f"  alasan : {reason}")
        print(f"  sumber : {source}")
    if notes:
        print("-" * 72)
        print("Detail:")
        for n in notes:
            print("  -", n)
    print("=" * 72)
    print("Kesimpulan:", "READY FOR REVIEW" if ready else "NOT READY")
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())

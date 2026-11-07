# Migrasi Billing Anthropic USD — Catatan Implementasi

Status: implementasi lokal selesai, SQL migration **belum dieksekusi**,
cutover runtime **belum dilakukan**.

## 1. Ringkasan keputusan

- Dua mata uang terpisah penuh: `IDR` (TokenMurah lama) dan `USD` (Anthropic).
- Currency ditentukan dari **prefix model**, bukan dari tier:
  - `tokenmurah/...` → `IDR`
  - `anthropic/...` → `USD`
  - prefix lain → `UnknownModelPricingError`, reservasi gagal (fail-safe).
- Budget USD global **5.00 USD per 30 hari bergulir**. Tidak ada hard limit per tier.
- Alokasi per tier hanya untuk monitoring.
- Thinking token disimpan sebagai metadata dan **tidak** ditambahkan ke biaya.
- Cache write tanpa TTL dihitung memakai tarif write 1 jam (konservatif) dan
  ditandai `cache_ttl_estimated = true`.
- Tidak ada fallback otomatis ke TokenMurah, tidak ada auto reload.

## 2. Tarif USD per 1 juta token

| Keluarga | Input | Output | Cache write 5m | Cache write 1h | Cache read |
|---|---|---|---|---|---|
| Haiku 5.5 (≤ 100.000 token) | 0.10 | 0.50 | 0.125 | 0.20 | 0.01 |
| Haiku 5.5 (> 100.000 token) | 0.50 | 2.50 | 0.625 | 1.00 | 0.05 |
| Sonnet 5.5 | 2.00 | 10.00 | 2.50 | 4.00 | 0.10 |
| Opus 5.5 | 4.00 | 20.00 | 5.00 | 8.00 | 0.20 |

Ambang long-context dihitung dari **total input prompt** =
`input_tokens + cache_read + cache_write_5m + cache_write_1h`.
Bila melewati ambang, seluruh kategori request memakai tarif long-context.

## 3. File yang berubah

| File | Perubahan |
|---|---|
| `app/config.py` | Tambah setting budget USD, ambang alert, dan 20 tarif USD. |
| `app/database/models.py` | `AIUsageLog`: +`billing_currency`, `cost_usd`, `reserved_usd`, `cache_write_5m_tokens`, `cache_write_1h_tokens`, `thinking_tokens`, `cache_ttl_estimated`, `analysis_log_id`. Model baru: `AIUsageAlert`, `AIPendingMessage`. |
| `app/ai/cost_guard.py` | Resolusi currency/provider, tarif USD, reservasi dua mata uang, finalisasi, alert 70/85/95 dengan dedup, snapshot USD, context analysis_log_id. |
| `app/ai/omniroute_client.py` | Normalisasi `thinking_tokens`, split TTL cache, metadata `cache_ttl_known`; teruskan usage baru ke finalize; alert USD best-effort; field `cost_usd`/`billing_currency` di respons. |
| `app/ai/ai_router.py` | `can_use_expensive_tier` sadar-currency (USD tanpa batas lunak Rp, model tak dikenal ditolak). `_attempt_record` mencatat currency dan biaya. |
| `app/ai/pipeline.py` | Set context `analysis_log_id`; status budget-block masuk `ai_pending_messages` dalam transaksi yang sama. |
| `app/ai/pending_queue.py` | **Baru.** Enqueue idempoten, claim atomik `FOR UPDATE SKIP LOCKED`, recovery claim menggantung, retry manual, discard. Tidak ada worker. |
| `app/ai/billing_report.py` | **Baru.** Laporan per mata uang, per model, per `analysis_log_id`, proyeksi 30 hari. |
| `migrations/2026_11_04_anthropic_usd_billing.sql` | **Draft.** Belum dieksekusi. |
| `test_anthropic_billing.py` | **Baru.** 33 test mock. |
| `test_omniroute_client.py` | Ekspektasi `normalize_usage` diperbarui + 2 test baru (TTL split, thinking tidak dobel). |

File yang **tidak** diubah: `message_aggregator.py`, `approval/*`, `execution/*`,
`signals/*`, `monitoring/*`, prompt/rulebook, `.env`.

## 4. Perubahan perilaku yang perlu direview

1. `normalize_usage()` kini mengembalikan 4 kunci tambahan:
   `cache_write_5m_tokens`, `cache_write_1h_tokens`, `thinking_tokens`,
   `cache_ttl_known`. Pemanggil yang membandingkan dict persis perlu menyesuaikan.
2. `call_ai()` mengembalikan `cost`, `cost_usd`, `billing_currency`; `cost_rp`
   bernilai `None` untuk jalur USD.
3. `can_use_expensive_tier()` mengembalikan `False` untuk model tanpa tarif
   dikenal (sebelumnya tidak memeriksa identitas model).
4. `finalize_ai_request()` menerima argumen baru dengan default, sehingga
   pemanggil lama tetap kompatibel.
5. `reserve_ai_request()` menerima `model=` dan `analysis_log_id=` opsional.

## 5. Desain antrean pesan (ai_pending_messages)

- `dedup_key = "<chat_id>:<telegram_message_id>"` unik → pesan sama tidak antre dua kali.
- Status: `PENDING`, `CLAIMED`, `ANALYZED`, `DISCARDED`.
- Claim: `SELECT ... FOR UPDATE SKIP LOCKED`, `attempts += 1`, `claimed_by`/`claimed_at`.
- Recovery restart: `release_stale_claims()` mengembalikan claim lebih tua dari 30 menit.
- Retry manual: `mark_pending_retry()` memasang `next_attempt_at`.
- **Tidak** ada worker inference baru. Worker belum boleh diaktifkan.
- Outbox notifikasi tetap terpisah: `telegram_notification_outbox` hanya untuk pengiriman.

## 6. Langkah manual berikutnya (belum dilakukan)

1. Review SQL draft, lalu eksekusi manual di Supabase.
2. Jalankan `pytest --ignore=test_ai_call.py` setelah migrasi DB.
3. Isi kredensial/prefix provider setelah review; `.env` tidak diubah oleh Hermes.
4. Cutover model runtime (FAST/REASONING/ESCALATION ke `anthropic/...`)
   dilakukan manual dengan persetujuan eksplisit.
5. Aktifkan `AI_PIPELINE_ENABLED` hanya setelah Cost Guard dan mock test
   diverifikasi di database nyata.

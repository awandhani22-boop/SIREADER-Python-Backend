
import json
from decimal import Decimal

from app.config import settings
from app.ai.provider_gateway import call_ai
from app.ai.cost_guard import (
    BudgetExceededError,
    CURRENCY_USD,
    UnknownModelPricingError,
    get_daily_usage,
    estimate_request_cost,
    resolve_billing,
)


# ============================================================
# SYSTEM PROMPT
# ============================================================


SYSTEM_PROMPT = """
Kamu adalah SIREADER, pembaca pesan mentor trading.

TUGAS UTAMA:
1. Baca seluruh rangkaian pesan sesuai urutan waktu Telegram.
2. Bedakan sinyal baru, permintaan entry, zona entry, konfirmasi,
   syarat entry, rencana re-entry, instruksi Skip, update posisi,
   TP, SL, dan komentar.
3. Jangan mengarang harga, arah, SL, TP, atau kondisi.
4. Ekstrak hanya informasi yang didukung pesan sumber.
5. Setiap informasi hasil ekstraksi wajib mempunyai kutipan
   persis dari pesan sumber.
6. Jangan menganggap pesan berdekatan pasti satu sinyal.
7. Sumber adalah satu chat mentor anonim.
8. Analisis hanya teks pesan dan riwayat edit teks.
   Abaikan semua media.
9. Jangan membuat keputusan transaksi atau mengirim order.
10. Jangan menganggap pesan mentor sebagai bukti status transaksi MT5.

KLASIFIKASI PESAN:

Setiap pesan dapat memiliki metadata:
- message_role
- message_roles
- trade_episode_id
- trade_episode_status

Metadata tersebut dibuat oleh sistem Python.

Gunakan metadata sebagai PETUNJUK KONTEKS,
bukan sebagai bukti transaksi.

Arti message_roles:

NEW_SIGNAL:
Sinyal baru yang disebutkan mentor.
Bisa berupa sinyal lengkap atau informasi zona yang
dipasangkan dengan permintaan entry sebelumnya.

ENTRY_REQUEST:
Mentor meminta atau mengisyaratkan BUY/SELL,
tetapi zona entry mungkin belum diberikan.
Contoh:
- Try buy
- Try sell
- Yuk sell again
- Buy again
- Sell again

PENTING:
ENTRY_REQUEST bukan konfirmasi bahwa order sudah boleh dikirim.
Jika zona belum tersedia, simpulkan bahwa permintaan masih
menunggu zona. Jangan mengarang zona atau harga entry.

ENTRY_CONFIRMATION:
Konfirmasi entry lanjutan yang benar-benar dinyatakan mentor.
Jangan menyimpulkan konfirmasi hanya dari kata BUY atau SELL.

REENTRY_PLAN:
Mentor baru merencanakan atau mencari peluang re-entry.
Contoh:
- Ntar ku carikan re entry
- Nanti cari re-entry
- Tunggu peluang re-entry

REENTRY_PLAN bukan instruksi entry aktual.
Jangan membuat sinyal atau order dari rencana tersebut.

PRICE_ZONE:
Pesan yang berisi rentang atau zona harga.
Periksa pesan sebelumnya dan trade_episode_id untuk mengetahui
apakah zona tersebut melengkapi ENTRY_REQUEST.
Jangan mengarang arah jika arah tidak ditemukan dalam pesan
atau konteks episode yang benar-benar terkait.

ENTRY_CONDITION:
Syarat entry seperti menunggu CISD, konfirmasi candle,
atau kondisi teknikal lainnya.

SETUP_HINT:
Petunjuk untuk mencari peluang BUY atau SELL.
Bukan otomatis instruksi entry.

SKIP:
Instruksi mentor untuk melewati atau membatalkan sinyal terkait.
Ekstrak instruksi Skip jika tertulis.
Periksa episode dan konteks pesan untuk mengidentifikasi
sinyal yang dimaksud.
Jangan menganggap Skip selalu berarti menutup semua posisi.
Jika target Skip tidak jelas, nyatakan ketidakpastian.

STOP_LOSS:
Informasi stop loss yang disebutkan mentor.
Bukan bukti bahwa posisi sudah terkena SL.

TAKE_PROFIT:
Informasi target take profit yang disebutkan mentor.
Bukan bukti bahwa posisi sudah mencapai TP.

PARTIAL_TP:
Informasi TP parsial atau target bertingkat.
Jangan menganggap seluruh posisi sudah ditutup.

POSITION_UPDATE:
Perkembangan posisi seperti jumlah pips, floating,
running, atau kondisi market.

POSITION_MANAGEMENT:
Instruksi mengamankan profit, BE, partial,
atau memindahkan SL.

POSITION_CLOSE:
Pernyataan atau instruksi penutupan posisi.
Bedakan instruksi untuk menutup posisi dari laporan bahwa
posisi sudah ditutup.
Jangan menganggap posisi MT5 benar-benar sudah tertutup
hanya berdasarkan klasifikasi pesan ini.

COMMENTARY:
Komentar umum yang bukan instruksi trading.

UNKNOWN:
Pesan yang perannya belum dapat dipastikan.

ATURAN EPISODE TRADE:

1. Pesan dengan trade_episode_id yang sama merupakan
   kandidat bagian dari satu rangkaian trade.

2. Jangan menggabungkan dua episode berbeda.

3. NEW_SIGNAL biasanya memulai rangkaian trade baru.

4. ENTRY_REQUEST menandakan permintaan entry yang mungkin
   masih menunggu zona.

5. PRICE_ZONE dapat melengkapi ENTRY_REQUEST jika hubungan
   konteks dan episode mendukungnya.

6. REENTRY_PLAN hanya rencana, bukan entry aktual.

7. ENTRY_CONFIRMATION, ENTRY_CONDITION, STOP_LOSS,
   TAKE_PROFIT, PARTIAL_TP, POSITION_UPDATE,
   POSITION_MANAGEMENT, dan POSITION_CLOSE dapat berkaitan
   dengan episode tertentu.

8. SKIP harus dikaitkan dengan sinyal atau episode yang tepat.
   Jangan membatalkan episode lain hanya karena waktunya berdekatan.

9. Pesan tanpa trade_episode_id tidak boleh dipaksakan
   menjadi bagian episode lain.

10. Jika metadata episode bertentangan dengan isi pesan,
    prioritaskan isi pesan asli dan nyatakan ketidakpastian.

11. Jangan menganggap pesan CLOSE atau POSITION_CLOSE
    otomatis membuktikan transaksi MT5 sudah ditutup.

12. Angka pips tanpa konteks tidak boleh dianggap TP,
    SL, harga entry, atau instruksi penutupan.

13. Jangan menganggap setiap pesan BUY atau SELL
    sebagai sinyal baru.

14. Jangan menghubungkan pesan hanya berdasarkan kedekatan waktu.

15. Riwayat edit dapat membantu memahami perubahan pesan.
    Jangan mencampur isi pesan lama dan baru tanpa menjelaskan
    bahwa informasi berasal dari riwayat edit.

KONTEKS RULEBOOK MENTOR:

- Zona BUY/SELL tanpa syarat tunggu CISD berarti entry
  dapat diproses berdasarkan zona oleh layer keputusan Python.

- Jika mentor menyebut "lowrisk entry CISD", terdapat syarat
  menunggu validasi CISD M1.

- Mode Auto Approval dan Manual Approval ditentukan oleh
  konfigurasi sistem, bukan oleh AI Router.

- Dalam Auto Approval, syarat menunggu CISD diabaikan
  oleh layer keputusan. Zona entry tetap wajib tersedia.

- Dalam Manual Approval, syarat CISD harus dikenali dan
  diteruskan untuk meminta keputusan pengguna.
  Jangan memutuskan sendiri apakah pengguna memilih AI
  atau entry manual.

- "Try buy", "Try sell", "buy again", dan "sell again"
  tidak berarti order boleh langsung dikirim jika zona
  belum tersedia.

- "Buy now" atau "Sell now" tanpa zona tetap harus menunggu
  zona dari mentor. Jangan mengarang harga entry.

- "Ntar ku carikan re entry" adalah rencana.
  Bukan instruksi untuk membuka posisi.

- Jangan menghitung midpoint, jarak pip, validitas harga,
  atau keputusan limit/market order dalam tahap ekstraksi ini.
  Perhitungan tersebut dilakukan oleh layer Python berikutnya.

- Jangan membuat SL atau TP berdasarkan asumsi.
  Jika SL/TP tidak disebutkan, jangan mengisi field tersebut.

- Aturan sistem SIREADER mengenai SL tetap 30 pips
  diterapkan oleh layer keputusan Python, bukan dengan
  mengarang stop_loss_text.

- Target TP mentor dapat berupa 60, 100, 200, 300,
  atau 500 pips, tetapi hanya ekstrak target yang benar-benar
  disebutkan dalam pesan terkait. Jangan otomatis mengisi
  semua target tersebut.

- Informasi +30 pips, BE, atau partial hanya diekstrak
  jika benar-benar dinyatakan mentor.

- "Hit SL", "SL kena", "TP hit", dan "TP 100 pips"
  merupakan informasi hasil atau perkembangan trade.
  Jangan menganggapnya sebagai bukti status posisi MT5.

- MT5 merupakan sumber kebenaran untuk status order,
  posisi terbuka, posisi tertutup, dan hasil transaksi.

- Jika mentor mengatakan Skip, ekstrak instruksi tersebut.
  Jangan menentukan sendiri posisi mana yang harus ditutup
  jika hubungan sinyal tidak jelas.

- Jika informasi tidak cukup, nyatakan tidak cukup.

ATURAN BUKTI:

- Teks pesan asli merupakan sumber utama.
- Riwayat edit boleh menjadi sumber evidence.
- Metadata klasifikasi bukan evidence.
- trade_episode_id bukan evidence.
- message_role bukan evidence.
- Kutipan harus sama persis dengan teks sumber.
- Jangan mengubah ejaan atau memperbaiki kutipan.
- Jika satu kutipan mendukung beberapa field,
  buat evidence terpisah untuk setiap field.
- Jangan mengambil bukti dari ringkasan AI sebelumnya.

Balas JSON valid saja. Jangan gunakan Markdown.

SCHEMA WAJIB:

{
  "status": "CLEAR|NEEDS_REASONING|UNRESOLVED|NO_SIGNAL",
  "confidence": 0.0,
  "needs_reasoning": false,
  "needs_escalation": false,
  "critical_ambiguity": false,
  "summary": "ringkasan singkat",
  "extracted": {
    "direction": null,
    "entry_zone_text": null,
    "explicit_entry_text": null,
    "stop_loss_text": null,
    "take_profit_texts": [],
    "conditions": [],
    "action_instructions": []
  },
  "evidence": [
    {
      "message_id": 123,
      "field": "direction",
      "quote": "kutipan persis dari pesan"
    }
  ],
  "uncertainties": []
}

KETENTUAN OUTPUT:

- direction hanya BUY, SELL, atau null.
- confidence angka 0 sampai 1.
- needs_reasoning, needs_escalation, critical_ambiguity
  harus boolean.
- Gunakan evidence untuk setiap field yang diekstrak.
- Quote wajib sama persis dengan teks pesan sumber.
- Jika tidak ada sinyal, gunakan status NO_SIGNAL.
- Jika instruksi bertentangan, gunakan UNRESOLVED.

DEFINISI FIELD:

1. direction:
   Hanya arah BUY atau SELL yang disebutkan eksplisit.

2. entry_zone_text:
   Hanya rentang atau zona harga entry yang tertulis
   pada sumber.

3. explicit_entry_text:
   Kutipan instruksi entry eksplisit seperti buy now
   atau sell now.
   Permintaan entry yang masih menunggu zona tidak boleh
   dianggap sebagai instruksi entry langsung.

4. stop_loss_text:
   Hanya informasi stop loss yang benar-benar disebutkan.

5. take_profit_texts:
   Hanya target take profit yang disebutkan.

6. conditions:
   Syarat entry, misalnya menunggu CISD
   atau konfirmasi candle.

7. action_instructions:
   Instruksi tindakan non-entry yang terpisah dari arah
   dan informasi entry.
   Termasuk instruksi pengelolaan posisi seperti close,
   partial, BE, memindahkan SL, serta instruksi Skip
   atau membatalkan sinyal.
   Ekstrak hanya jika tertulis secara eksplisit.
   Jangan menyimpulkan bahwa tindakan sudah berhasil dilakukan.
   Jangan mengulang direction, entry, SL, atau TP.

ATURAN STATUS:

CLEAR:
Informasi cukup jelas dan tidak terdapat ambiguitas penting.

NEEDS_REASONING:
Diperlukan pemeriksaan konteks lebih lanjut.

UNRESOLVED:
Terdapat konflik atau ambiguitas yang belum terselesaikan.

NO_SIGNAL:
Tidak terdapat sinyal trading yang dapat diekstrak.

ATURAN EVIDENCE:

- Field pada evidence harus sama persis dengan nama field
  di dalam extracted.
- Setiap field extracted yang memiliki nilai wajib
  mempunyai minimal satu evidence dengan nama field sama.
- Setiap kutipan harus sama persis dengan teks sumber.
- Jika satu kutipan mendukung beberapa field,
  buat evidence terpisah.
- Jangan mengisi field berdasarkan dugaan.
- Jika tidak ada instruksi tindakan eksplisit,
  action_instructions harus berupa [].

ATURAN KETIDAKPASTIAN:

Jika arah, harga, hubungan episode, atau instruksi mentor
belum jelas, jangan memaksakan kesimpulan.

Masukkan alasan ketidakpastian ke uncertainties.

Jika terdapat ambiguitas penting yang dapat mengubah
interpretasi sinyal, gunakan UNRESOLVED atau NEEDS_REASONING.

Jangan menghasilkan keputusan order.
"""


class AIOutputError(Exception):
    pass


# ============================================================
# PREPARE BUNDLE
# ============================================================

def prepare_bundle(bundle):
    """
    Menyiapkan payload untuk AI.

    Mempertahankan:
    - Teks asli dan ID Telegram.
    - Waktu pesan dan reply.
    - Riwayat edit.
    - Klasifikasi peran pesan.
    - Identitas dan status episode trade.

    Metadata klasifikasi hanya menjadi konteks,
    bukan bukti ekstraksi.
    """

    if not isinstance(bundle, dict):
        raise ValueError(
            "Bundle harus berupa dictionary."
        )

    source_messages = bundle.get("messages")

    if (
        not isinstance(source_messages, list)
        or not source_messages
    ):
        raise ValueError(
            "Bundle tidak memiliki pesan."
        )

    prepared = []

    for item in source_messages:

        if not isinstance(item, dict):
            raise ValueError(
                "Format pesan dalam bundle tidak valid."
            )

        if "telegram_message_id" not in item:
            raise ValueError(
                "Telegram message ID tidak ditemukan."
            )

        edits = item.get("edit_history") or []

        message = {
            "message_id": item["telegram_message_id"],
            "text": item.get("text") or "",
        }

        if item.get("telegram_date"):
            message["telegram_date"] = item["telegram_date"]

        if item.get("reply_to_id") is not None:
            message["reply_to_id"] = item["reply_to_id"]

        if edits:
            if not isinstance(edits, list):
                raise ValueError(
                    "Edit history harus berupa daftar."
                )

            message["edit_history"] = [
                {
                    "old_text": edit.get("old_text") or "",
                    "new_text": edit.get("new_text") or "",
                }
                for edit in edits
            ]

        # Klasifikasi pesan.
        roles = item.get("message_roles")

        if isinstance(roles, list):
            message["message_roles"] = [
                role
                for role in roles
                if isinstance(role, str)
            ]

        if isinstance(item.get("message_role"), str):
            message["message_role"] = item["message_role"]

        # Episode trade.
        if isinstance(item.get("trade_episode_id"), str):
            message["trade_episode_id"] = (
                item["trade_episode_id"]
            )

        if isinstance(item.get("trade_episode_status"), str):
            message["trade_episode_status"] = (
                item["trade_episode_status"]
            )

        prepared.append(message)

    return {
        "target_message_id": bundle.get("target_message_id"),
        "messages": prepared,
    }


# ============================================================
# JSON EXTRACTION
# ============================================================

def extract_json_object(content):
    """
    Mengambil satu objek JSON dari respons AI.

    Mendukung:
    - JSON biasa.
    - Markdown code fence.
    - Teks tambahan sebelum JSON.
    - Teks tambahan setelah JSON.

    String dan karakter escape diperhatikan agar kurung
    kurawal di dalam teks tidak merusak pencarian objek.
    """

    if not isinstance(content, str):
        raise AIOutputError(
            "Output AI bukan teks."
        )

    content = content.strip().lstrip("\ufeff")

    if not content:
        raise AIOutputError(
            "Output AI kosong."
        )

    start = content.find("{")

    if start == -1:
        preview = content[:300].replace("\n", " ")

        raise AIOutputError(
            "AI tidak mengembalikan objek JSON. "
            f"Awal respons: {preview!r}"
        )

    depth = 0
    in_string = False
    escaped = False
    end = None

    for index in range(start, len(content)):
        char = content[index]

        if in_string:

            if escaped:
                escaped = False

            elif char == "\\":
                escaped = True

            elif char == '"':
                in_string = False

        else:

            if char == '"':
                in_string = True

            elif char == "{":
                depth += 1

            elif char == "}":
                depth -= 1

                if depth == 0:
                    end = index + 1
                    break

                if depth < 0:
                    break

    if end is None:
        preview = content[start:start + 300].replace(
            "\n",
            " ",
        )

        raise AIOutputError(
            "Objek JSON tidak lengkap atau terpotong. "
            f"Awal objek: {preview!r}"
        )

    json_content = content[start:end]

    try:
        result = json.loads(json_content)

    except json.JSONDecodeError as error:
        preview = json_content[:300].replace(
            "\n",
            " ",
        )

        raise AIOutputError(
            "AI mengembalikan JSON dengan format tidak valid. "
            f"Baris: {error.lineno}, "
            f"Kolom: {error.colno}, "
            f"Detail: {error.msg}, "
            f"Awal respons: {preview!r}"
        ) from error

    if not isinstance(result, dict):
        raise AIOutputError(
            "Struktur output AI tidak valid."
        )

    return result


# ============================================================
# PARSE MODEL OUTPUT
# ============================================================

def parse_model_output(content, bundle):
    """
    Memvalidasi JSON dan memastikan evidence berasal dari sumber.

    Tidak mempercayai hasil ekstraksi AI sebelum seluruh
    validasi selesai.
    """

    result = extract_json_object(content)

    # --------------------------------------------------------
    # 1. VALIDASI STATUS
    # --------------------------------------------------------

    allowed_status = {
        "CLEAR",
        "NEEDS_REASONING",
        "UNRESOLVED",
        "NO_SIGNAL",
    }

    if result.get("status") not in allowed_status:
        raise AIOutputError(
            "Status analisis tidak valid."
        )

    # --------------------------------------------------------
    # 2. VALIDASI CONFIDENCE
    # --------------------------------------------------------

    confidence = result.get("confidence")

    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        raise AIOutputError(
            "Confidence harus antara 0 dan 1."
        )

    # --------------------------------------------------------
    # 3. VALIDASI BOOLEAN
    # --------------------------------------------------------

    boolean_fields = (
        "needs_reasoning",
        "needs_escalation",
        "critical_ambiguity",
    )

    for field in boolean_fields:

        if not isinstance(result.get(field), bool):
            raise AIOutputError(
                f"Field {field} harus boolean."
            )

    # --------------------------------------------------------
    # 4. VALIDASI SUMMARY
    # --------------------------------------------------------

    if not isinstance(result.get("summary"), str):
        raise AIOutputError(
            "Summary tidak valid."
        )

    # --------------------------------------------------------
    # 5. VALIDASI EXTRACTED
    # --------------------------------------------------------

    extracted = result.get("extracted")

    required_extracted_fields = {
        "direction",
        "entry_zone_text",
        "explicit_entry_text",
        "stop_loss_text",
        "take_profit_texts",
        "conditions",
        "action_instructions",
    }

    if not isinstance(extracted, dict):
        raise AIOutputError(
            "Extracted tidak valid."
        )

    missing_fields = (
        required_extracted_fields
        - set(extracted.keys())
    )

    if missing_fields:
        raise AIOutputError(
            "Field extracted tidak lengkap: "
            + ", ".join(sorted(missing_fields))
        )

    # --------------------------------------------------------
    # 6. VALIDASI DIRECTION
    # --------------------------------------------------------

    direction = extracted["direction"]

    if direction not in (None, "BUY", "SELL"):
        raise AIOutputError(
            "Direction tidak valid."
        )

    # --------------------------------------------------------
    # 7. VALIDASI LIST
    # --------------------------------------------------------

    list_fields = (
        "take_profit_texts",
        "conditions",
        "action_instructions",
    )

    for field in list_fields:

        value = extracted[field]

        if (
            not isinstance(value, list)
            or not all(
                isinstance(item, str)
                for item in value
            )
        ):
            raise AIOutputError(
                f"Field {field} harus berupa daftar teks."
            )

    # --------------------------------------------------------
    # 8. VALIDASI STRING
    # --------------------------------------------------------

    string_fields = (
        "entry_zone_text",
        "explicit_entry_text",
        "stop_loss_text",
    )

    for field in string_fields:

        value = extracted[field]

        if (
            value is not None
            and not isinstance(value, str)
        ):
            raise AIOutputError(
                f"Field {field} tidak valid."
            )

    # --------------------------------------------------------
    # 9. VALIDASI UNCERTAINTIES
    # --------------------------------------------------------

    uncertainties = result.get("uncertainties")

    if (
        not isinstance(uncertainties, list)
        or not all(
            isinstance(item, str)
            for item in uncertainties
        )
    ):
        raise AIOutputError(
            "Uncertainties tidak valid."
        )

    # --------------------------------------------------------
    # 10. VALIDASI BUNDLE SUMBER
    # --------------------------------------------------------

    if not isinstance(bundle, dict):
        raise AIOutputError(
            "Bundle sumber tidak valid."
        )

    source_messages = bundle.get("messages")

    if not isinstance(source_messages, list):
        raise AIOutputError(
            "Pesan sumber tidak valid."
        )

    # --------------------------------------------------------
    # 11. KUMPULKAN TEKS SUMBER
    # --------------------------------------------------------

    source_texts = {}

    for message in source_messages:

        if not isinstance(message, dict):
            raise AIOutputError(
                "Format pesan sumber tidak valid."
            )

        message_id = message.get("message_id")

        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
        ):
            raise AIOutputError(
                "Message ID sumber tidak valid."
            )

        texts = [
            message.get("text") or ""
        ]

        edits = message.get("edit_history") or []

        if not isinstance(edits, list):
            raise AIOutputError(
                "Riwayat edit tidak valid."
            )

        for edit in edits:

            if not isinstance(edit, dict):
                raise AIOutputError(
                    "Format riwayat edit tidak valid."
                )

            texts.extend([
                edit.get("old_text") or "",
                edit.get("new_text") or "",
            ])

        source_texts[message_id] = texts

    # --------------------------------------------------------
    # 12. VALIDASI EVIDENCE
    # --------------------------------------------------------

    evidence = result.get("evidence")

    if not isinstance(evidence, list):
        raise AIOutputError(
            "Evidence harus berupa daftar."
        )

    evidence_fields = set()

    for item in evidence:

        if not isinstance(item, dict):
            raise AIOutputError(
                "Format evidence tidak valid."
            )

        message_id = item.get("message_id")
        field = item.get("field")
        quote = item.get("quote")

        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id not in source_texts
        ):
            raise AIOutputError(
                "Evidence merujuk pesan yang tidak ada."
            )

        if (
            not isinstance(field, str)
            or not field.strip()
            or not isinstance(quote, str)
        ):
            raise AIOutputError(
                "Evidence tidak lengkap."
            )

        if not quote:
            raise AIOutputError(
                "Kutipan evidence tidak boleh kosong."
            )

        if not any(
            quote in source_text
            for source_text in source_texts[message_id]
        ):
            raise AIOutputError(
                "Kutipan evidence tidak cocok "
                "dengan pesan sumber."
            )

        evidence_fields.add(field)

    # --------------------------------------------------------
    # 13. SETIAP FIELD EKSTRAKSI WAJIB PUNYA EVIDENCE
    # --------------------------------------------------------

    for field in required_extracted_fields:

        value = extracted[field]

        has_value = (
            value is not None
            and value != ""
            and value != []
        )

        if (
            has_value
            and field not in evidence_fields
        ):
            raise AIOutputError(
                f"Field {field} tidak memiliki evidence."
            )

    # --------------------------------------------------------
    # 14. NORMALISASI
    # --------------------------------------------------------

    result["confidence"] = float(confidence)

    return result


# ============================================================
# BUILD MESSAGES
# ============================================================

def build_messages(bundle, stage, previous_analysis=None):

    payload = prepare_bundle(bundle)

    user_payload = {
        "stage": stage,
        "instructions": (
            "Analisis rangkaian pesan berdasarkan urutan waktu. "
            "Gunakan message_roles dan trade_episode_id sebagai "
            "petunjuk hubungan pesan. Jangan menjadikan metadata "
            "sebagai evidence. Ekstrak informasi dan kutipan "
            "persis sesuai schema."
        ),
        "bundle": payload,
    }

    if previous_analysis is not None:

        user_payload["previous_analysis"] = previous_analysis

        user_payload["instructions"] = (
            "Tinjau ulang analisis sebelumnya. "
            "Periksa hubungan episode, konteks, bukti, "
            "dan ambiguitas. Perbaiki hanya jika ada dasar "
            "dari pesan sumber. Metadata klasifikasi hanya "
            "petunjuk dan bukan evidence."
        )

    return [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": json.dumps(
                user_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]


# ============================================================
# EXPENSIVE TIER BUDGET CHECK
# ============================================================

def can_use_expensive_tier(tier, messages):
    """
    Batas lunak harian Rp10.000 hanya berlaku untuk jalur IDR lama.
    Untuk model Anthropic (USD), tidak ada batas lunak per hari:
    hard limit USD 5.00 dijaga Cost Guard secara atomik.
    Model tanpa tarif dikenal selalu ditolak (gagal aman).
    """

    config = {
        "REASONING": (
            settings.AI_MAX_OUTPUT_REASONING,
            settings.AI_MODEL_REASONING,
        ),
        "ESCALATION": (
            settings.AI_MAX_OUTPUT_ESCALATION,
            settings.AI_MODEL_ESCALATION,
        ),
    }

    if tier not in config:
        raise ValueError(
            f"Tier tidak dikenal: {tier}"
        )

    max_output, model = config[tier]

    try:
        billing = resolve_billing(model)
    except UnknownModelPricingError:
        return False

    if billing["currency"] == CURRENCY_USD:
        return True

    serialized = json.dumps(
        messages,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    estimate = estimate_request_cost(
        tier=tier,
        prompt_text=serialized,
        max_output_tokens=max_output,
    )

    daily = get_daily_usage()

    projected = Decimal(
        str(daily["projected_cost_rp"])
    )

    reserved = Decimal(
        str(estimate["reserved_cost_rp"])
    )

    target = Decimal(
        str(settings.AI_TARGET_DAILY_BUDGET_RP)
    )

    return projected + reserved <= target


# ============================================================
# RUN AI TIER
# ============================================================

def _run_tier(tier, messages):

    config = {
        "FAST": settings.AI_MAX_OUTPUT_FAST,
        "REASONING": settings.AI_MAX_OUTPUT_REASONING,
        "ESCALATION": settings.AI_MAX_OUTPUT_ESCALATION,
    }

    if tier not in config:
        raise ValueError(
            f"Tier tidak dikenal: {tier}"
        )

    response = call_ai(
        tier=tier,
        messages=messages,
        max_output_tokens=config[tier],
    )

    return {
        "analysis": response,
        "parsed": None,
    }


# ============================================================
# CHECKPOINT
# ============================================================

def _make_resume_state(
    next_tier,
    attempts,
    path="normal",
    fast_analysis=None,
    sonnet_analysis=None,
    fast_refusal_excerpt=None,
):
    return {
        "version": 1,
        "next_tier": next_tier,
        "path": path,
        "fast_analysis": fast_analysis,
        "sonnet_analysis": sonnet_analysis,
        "fast_refusal_excerpt": fast_refusal_excerpt,
        "attempts": attempts,
    }


def _emit_checkpoint(checkpoint_callback, state):
    if checkpoint_callback is not None:
        checkpoint_callback(state)


def _validate_resume_state(state):
    if state is None:
        return

    if not isinstance(state, dict):
        raise ValueError(
            "resume_state harus berupa dictionary."
        )

    if state.get("version") != 1:
        raise ValueError(
            "Versi resume_state tidak dikenal."
        )

    next_tier = state.get("next_tier")
    path = state.get("path")

    if next_tier not in {
        "FAST",
        "REASONING",
        "ESCALATION",
    }:
        raise ValueError(
            "next_tier resume_state tidak valid."
        )

    if path not in {
        "normal",
        "refusal_fallback",
    }:
        raise ValueError(
            "path resume_state tidak valid."
        )

    attempts = state.get("attempts")

    if not isinstance(attempts, list):
        raise ValueError(
            "attempts resume_state tidak valid."
        )

    expected_attempts = {
        "FAST": [],
        "REASONING": ["FAST"],
        "ESCALATION": ["FAST", "REASONING"],
    }

    actual_attempts = []

    for attempt in attempts:

        if not isinstance(attempt, dict):
            raise ValueError(
                "Data attempt resume_state tidak valid."
            )

        tier = attempt.get("tier")

        if tier not in {
            "FAST",
            "REASONING",
            "ESCALATION",
        }:
            raise ValueError(
                "Tier attempt resume_state tidak valid."
            )

        request_id = attempt.get("request_id")

        if request_id is not None and (
            not isinstance(request_id, str)
            or not request_id.strip()
        ):
            raise ValueError(
                "request_id attempt tidak valid."
            )

        actual_attempts.append(tier)

    if actual_attempts != expected_attempts[next_tier]:
        raise ValueError(
            "Urutan attempt tidak sesuai dengan "
            "next_tier resume_state."
        )

    if next_tier == "REASONING":

        if path == "normal":

            if not isinstance(
                state.get("fast_analysis"),
                dict,
            ):
                raise ValueError(
                    "Checkpoint FAST tidak tersedia."
                )

            if state.get("fast_refusal_excerpt"):
                raise ValueError(
                    "Checkpoint normal tidak boleh "
                    "memiliki refusal excerpt."
                )

        else:

            if not state.get("fast_refusal_excerpt"):
                raise ValueError(
                    "Checkpoint refusal FAST tidak tersedia."
                )

            if state.get("fast_analysis") is not None:
                raise ValueError(
                    "Checkpoint refusal tidak boleh "
                    "memiliki fast_analysis."
                )

    if next_tier == "ESCALATION":

        if path != "normal":
            raise ValueError(
                "ESCALATION hanya diperbolehkan "
                "pada path normal."
            )

        if not isinstance(
            state.get("fast_analysis"),
            dict,
        ):
            raise ValueError(
                "Checkpoint FAST tidak tersedia."
            )

        if not isinstance(
            state.get("sonnet_analysis"),
            dict,
        ):
            raise ValueError(
                "Checkpoint Sonnet tidak tersedia."
            )

        if state.get("fast_refusal_excerpt"):
            raise ValueError(
                "Checkpoint ESCALATION tidak boleh "
                "memiliki refusal excerpt."
            )


# ============================================================
# MAIN AI ROUTER
# ============================================================

def _is_model_refusal(content):
    """Deteksi refusal eksplisit di awal respons."""

    if not isinstance(content, str):
        return False

    text = content.lstrip("\ufeff \t\r\n").casefold()
    text = text.replace("’", "'").replace("‘", "'")

    refusal_prefixes = (
        "i can't help",
        "i cannot help",
        "i can't assist",
        "i cannot assist",
        "i'm not able to assist",
        "i am not able to assist",
        "i'm unable to assist",
        "i am unable to assist",
        "i'm unable to help",
        "i am unable to help",
        "i can't provide",
        "i cannot provide",
        "i'm sorry, but i can't",
        "i am sorry, but i cannot",
    )

    return text.startswith(refusal_prefixes)


def _attempt_record(tier, response):
    """Membuat ringkasan pemanggilan tanpa payload sensitif."""

    analysis = response.get("analysis") or {}

    return {
        "tier": tier,
        "request_id": analysis.get("request_id"),
        "model": analysis.get("model"),
        "billing_currency": analysis.get(
            "billing_currency",
            "USD",
        ),
        "cost": str(
            analysis.get(
                "cost",
                analysis.get("cost_rp", "0"),
            )
        ),
        "cost_rp": str(
            analysis.get("cost_rp") or "0"
        ),
        "cost_usd": str(
            analysis.get("cost_usd") or "0"
        ),
        "usage": analysis.get("usage") or {},
    }


def _refused_result(tier, content, attempts, **extra):
    """Hasil proses REFUSED; bukan status JSON model."""

    return {
        "status": "REFUSED",
        "reason": "MODEL_EXPLICIT_REFUSAL",
        "refused_tier": tier,
        "refusal_excerpt": (content or "")[:600],
        "attempts": attempts,
        **extra,
    }


def _final_analysis_status(analysis):
    """Hasil dengan ketidakpastian wajib masuk manual review."""

    if analysis.get("status") == "NO_SIGNAL":
        return "NO_SIGNAL"

    uncertainties = analysis.get("uncertainties") or []

    has_uncertainties = any(
        isinstance(item, str) and item.strip()
        for item in uncertainties
    )

    needs_manual_review = (
        analysis.get("status") in {
            "NEEDS_REASONING",
            "UNRESOLVED",
        }
        or analysis.get("needs_reasoning") is True
        or analysis.get("needs_escalation") is True
        or analysis.get("critical_ambiguity") is True
        or analysis.get("confidence", 0.0) < 0.80
        or has_uncertainties
    )

    return (
        "MANUAL_REVIEW"
        if needs_manual_review
        else "ANALYZED"
    )


def _bundle_is_pure_commentary(bundle):
    """Skip model inference only when every item is explicitly safe commentary.

    Missing/unknown roles, replies, edits, episode links, or any trade role fail
    open to AI. Historical Telegram content is never deleted by this filter.
    """
    items = bundle.get("messages") if isinstance(bundle, dict) else None
    if not isinstance(items, list) or not items:
        return False
    for item in items:
        if not isinstance(item, dict):
            return False
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            return False
        if item.get("reply_to_id") is not None:
            return False
        if item.get("trade_episode_id") or item.get("trade_episode_status"):
            return False
        if item.get("edit_history") or item.get("is_edited"):
            return False
        roles = item.get("message_roles")
        if not isinstance(roles, list):
            role = item.get("message_role")
            roles = [role] if isinstance(role, str) else []
        if roles != ["COMMENTARY"]:
            return False
    return True


def _output_limit_result(tier, attempts, previous_analysis=None):
    """Fail closed on truncated JSON; a partial model response is never executable."""
    return {
        "status": "MANUAL_REVIEW",
        "reason": "MODEL_OUTPUT_LIMIT_REACHED",
        "truncated_tier": tier,
        "final_analysis": previous_analysis,
        "attempts": attempts,
        "partial_output_is_authoritative": False,
    }


def analyze_bundle(
    bundle,
    resume_state=None,
    checkpoint_callback=None,
):
    """
    Routing SIREADER dengan checkpoint.

    Resume hanya melanjutkan tier yang belum berhasil.
    Tier yang sudah berhasil tidak dipanggil ulang.
    """

    if resume_state is None and _bundle_is_pure_commentary(bundle):
        return {
            "status": "NO_SIGNAL",
            "final_analysis": {
                "status": "NO_SIGNAL",
                "confidence": 1.0,
                "needs_reasoning": False,
                "needs_escalation": False,
                "critical_ambiguity": False,
                "summary": "Python prefilter: all messages were explicitly classified as pure commentary.",
                "extracted": {
                    "direction": None,
                    "entry_zone_text": None,
                    "explicit_entry_text": None,
                    "stop_loss_text": None,
                    "take_profit_texts": [],
                    "conditions": [],
                    "action_instructions": [],
                },
                "evidence": [],
                "uncertainties": [],
            },
            "attempts": [],
            "routing": {
                "mode": "PYTHON_FILTER",
                "reason_code": "PURE_COMMENTARY",
                "model_calls": 0,
            },
        }

    prepared = prepare_bundle(bundle)
    _validate_resume_state(resume_state)

    attempts = []
    fast = None
    sonnet = None
    fast_refusal_excerpt = None
    path = "normal"
    next_tier = "FAST"

    if resume_state:

        attempts = list(resume_state["attempts"])
        next_tier = resume_state["next_tier"]
        path = resume_state["path"]

        fast = resume_state.get("fast_analysis")
        sonnet = resume_state.get("sonnet_analysis")

        fast_refusal_excerpt = resume_state.get(
            "fast_refusal_excerpt"
        )

    # ========================================================
    # LEVEL 1: FAST
    # ========================================================

    if next_tier == "FAST":

        fast_messages = build_messages(
            bundle,
            stage="FAST_INITIAL_ANALYSIS",
        )

        try:
            fast_response = _run_tier(
                "FAST",
                fast_messages,
            )

        except BudgetExceededError:

            state = _make_resume_state(
                next_tier="FAST",
                attempts=attempts,
            )

            return {
                "status": "BLOCKED_HARD_BUDGET",
                "attempts": attempts,
                "resume_state": state,
            }

        fast_content = fast_response["analysis"].get(
            "content",
            "",
        )

        attempts.append(
            _attempt_record(
                "FAST",
                fast_response,
            )
        )

        if fast_response["analysis"].get("finish_reason") == "max_tokens":
            return _output_limit_result("FAST", attempts)

        # FAST refusal: jalur fallback khusus.
        if _is_model_refusal(fast_content):

            fast_refusal_excerpt = fast_content[:600]
            path = "refusal_fallback"
            next_tier = "REASONING"

            state = _make_resume_state(
                next_tier="REASONING",
                path=path,
                attempts=attempts,
                fast_refusal_excerpt=fast_refusal_excerpt,
            )

            _emit_checkpoint(
                checkpoint_callback,
                state,
            )

        else:

            fast = parse_model_output(
                fast_content,
                prepared,
            )

            if fast["status"] == "NO_SIGNAL":
                return {
                    "status": "NO_SIGNAL",
                    "final_analysis": fast,
                    "attempts": attempts,
                }

            needs_reasoning = (
                fast["needs_reasoning"]
                or fast["status"] in {
                    "NEEDS_REASONING",
                    "UNRESOLVED",
                }
                or fast["confidence"] < 0.80
            )

            if not needs_reasoning:
                return {
                    "status": _final_analysis_status(fast),
                    "final_analysis": fast,
                    "attempts": attempts,
                }

            next_tier = "REASONING"

            state = _make_resume_state(
                next_tier="REASONING",
                path="normal",
                attempts=attempts,
                fast_analysis=fast,
            )

            _emit_checkpoint(
                checkpoint_callback,
                state,
            )

    # ========================================================
    # LEVEL 2: REASONING
    # ========================================================

    if next_tier == "REASONING":

        if path == "refusal_fallback":

            sonnet_messages = build_messages(
                bundle,
                stage="REFUSAL_FALLBACK",
            )

        else:

            sonnet_messages = build_messages(
                bundle,
                stage="REASONING_REVIEW",
                previous_analysis=fast,
            )

        if not can_use_expensive_tier(
            "REASONING",
            sonnet_messages,
        ):

            state = _make_resume_state(
                next_tier="REASONING",
                path=path,
                attempts=attempts,
                fast_analysis=fast,
                fast_refusal_excerpt=fast_refusal_excerpt,
            )

            return {
                "status": "MANUAL_REVIEW_BUDGET",
                "reason": (
                    "FALLBACK_BLOCKED_BY_SOFT_BUDGET"
                    if path == "refusal_fallback"
                    else "REASONING_BLOCKED_BY_SOFT_BUDGET"
                ),
                "final_analysis": fast,
                "fast_refusal_excerpt": fast_refusal_excerpt,
                "attempts": attempts,
                "resume_state": state,
            }

        try:

            sonnet_response = _run_tier(
                "REASONING",
                sonnet_messages,
            )

        except BudgetExceededError:

            state = _make_resume_state(
                next_tier="REASONING",
                path=path,
                attempts=attempts,
                fast_analysis=fast,
                fast_refusal_excerpt=fast_refusal_excerpt,
            )

            return {
                "status": "MANUAL_REVIEW_BUDGET",
                "reason": "REASONING_BLOCKED_BY_HARD_BUDGET",
                "final_analysis": fast,
                "fast_refusal_excerpt": fast_refusal_excerpt,
                "attempts": attempts,
                "resume_state": state,
            }

        sonnet_content = sonnet_response["analysis"].get(
            "content",
            "",
        )

        attempts.append(
            _attempt_record(
                "REASONING",
                sonnet_response,
            )
        )

        if sonnet_response["analysis"].get("finish_reason") == "max_tokens":
            return _output_limit_result("REASONING", attempts, previous_analysis=fast)

        if _is_model_refusal(sonnet_content):

            return _refused_result(
                "REASONING",
                sonnet_content,
                attempts,
                previous_refusal_tier=(
                    "FAST"
                    if path == "refusal_fallback"
                    else None
                ),
            )

        sonnet = parse_model_output(
            sonnet_content,
            prepared,
        )

        # Jalur refusal fallback tidak boleh diteruskan ke Opus.
        if path == "refusal_fallback":

            return {
                "status": _final_analysis_status(sonnet),
                "final_analysis": sonnet,
                "attempts": attempts,
                "fallback_from": "FAST_REFUSAL",
                "opus_skipped_after_refusal_fallback": True,
            }

        needs_opus = (
            sonnet["needs_escalation"]
            and sonnet["critical_ambiguity"]
            and sonnet["status"] == "UNRESOLVED"
        )

        if not needs_opus:

            return {
                "status": _final_analysis_status(sonnet),
                "final_analysis": sonnet,
                "attempts": attempts,
            }

        next_tier = "ESCALATION"

        state = _make_resume_state(
            next_tier="ESCALATION",
            path="normal",
            attempts=attempts,
            fast_analysis=fast,
            sonnet_analysis=sonnet,
        )

        _emit_checkpoint(
            checkpoint_callback,
            state,
        )

    # ========================================================
    # LEVEL 3: ESCALATION
    # ========================================================

    if next_tier == "ESCALATION":

        opus_messages = build_messages(
            bundle,
            stage="CRITICAL_AMBIGUITY_REVIEW",
            previous_analysis=sonnet,
        )

        if not can_use_expensive_tier(
            "ESCALATION",
            opus_messages,
        ):

            state = _make_resume_state(
                next_tier="ESCALATION",
                attempts=attempts,
                fast_analysis=fast,
                sonnet_analysis=sonnet,
            )

            return {
                "status": "MANUAL_REVIEW_BUDGET",
                "reason": "ESCALATION_BLOCKED_BY_SOFT_BUDGET",
                "final_analysis": sonnet,
                "attempts": attempts,
                "resume_state": state,
            }

        try:

            opus_response = _run_tier(
                "ESCALATION",
                opus_messages,
            )

        except BudgetExceededError:

            state = _make_resume_state(
                next_tier="ESCALATION",
                attempts=attempts,
                fast_analysis=fast,
                sonnet_analysis=sonnet,
            )

            return {
                "status": "MANUAL_REVIEW_BUDGET",
                "reason": "ESCALATION_BLOCKED_BY_HARD_BUDGET",
                "final_analysis": sonnet,
                "attempts": attempts,
                "resume_state": state,
            }

        opus_content = opus_response["analysis"].get(
            "content",
            "",
        )

        attempts.append(
            _attempt_record(
                "ESCALATION",
                opus_response,
            )
        )

        if opus_response["analysis"].get("finish_reason") == "max_tokens":
            return _output_limit_result("ESCALATION", attempts, previous_analysis=sonnet)

        if _is_model_refusal(opus_content):

            return _refused_result(
                "ESCALATION",
                opus_content,
                attempts,
            )

        opus = parse_model_output(
            opus_content,
            prepared,
        )

        return {
            "status": _final_analysis_status(opus),
            "final_analysis": opus,
            "attempts": attempts,
        }

    raise ValueError(
        f"Resume tier tidak ditangani: {next_tier}"
    )

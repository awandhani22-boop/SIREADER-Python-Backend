import json

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.database.connection import SessionLocal
from app.database.models import (
    AIAnalysisLog,
    ApprovalDecision,
)
from app.ai.message_aggregator import collect_message_bundle


PENDING_STATUS = "MANUAL_REVIEW"
ALLOWED_DECISIONS = {"APPROVED", "REJECTED"}


def _decode_result(raw):
    if not raw:
        return {}

    try:
        result = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"_parse_error": "JSON hasil analisis tidak valid."}

    if not isinstance(result, dict):
        return {"_parse_error": "Hasil analisis bukan objek JSON."}

    return result


def _final_analysis(result):
    final = result.get("final_analysis")

    if isinstance(final, dict):
        return final

    return result


def list_pending_reviews(limit=50):
    """
    Mengambil antrean analisis yang membutuhkan pemeriksaan manual.
    Tidak mengubah database.
    """

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 200
    ):
        raise ValueError("Limit harus berupa angka 1 sampai 200.")

    with SessionLocal() as db:
        logs = db.scalars(
            select(AIAnalysisLog)
            .where(AIAnalysisLog.status == PENDING_STATUS)
            .order_by(AIAnalysisLog.created_at.asc())
            .limit(limit)
        ).all()

        output = []

        for log in logs:
            result = _decode_result(log.result_json)
            final = _final_analysis(result)

            uncertainties = final.get("uncertainties") or []

            output.append({
                "analysis_log_id": log.id,
                "chat_id": log.chat_id,
                "target_message_id": log.target_message_id,
                "status": log.status,
                "created_at": (
                    log.created_at.isoformat()
                    if log.created_at else None
                ),
                "summary": final.get("summary"),
                "confidence": final.get("confidence"),
                "direction": (
                    (final.get("extracted") or {}).get("direction")
                ),
                "uncertainties": uncertainties,
                "parse_error": result.get("_parse_error"),
            })

        return output


def get_review_detail(analysis_log_id):
    """
    Mengambil detail analisis, evidence, sumber pesan teks,
    dan keputusan reviewer sebelumnya.
    """

    if (
        isinstance(analysis_log_id, bool)
        or not isinstance(analysis_log_id, int)
        or analysis_log_id <= 0
    ):
        raise ValueError("Analysis log ID tidak valid.")

    # Satu Session untuk log, keputusan, source bundle, dan Manual TP.
    # Menggabungkan beberapa round-trip menjadi satu checkout koneksi.
    # Seluruh hasil dimaterialisasi ke dict sebelum Session ditutup,
    # sehingga tidak ada ORM object yang bergantung pada Session.
    with SessionLocal() as db:
        log = db.scalar(
            select(AIAnalysisLog).where(
                AIAnalysisLog.id == analysis_log_id
            )
        )

        if not log:
            raise LookupError("Analisis tidak ditemukan.")

        result = _decode_result(log.result_json)

        decisions = db.scalars(
            select(ApprovalDecision)
            .where(
                ApprovalDecision.analysis_log_id == analysis_log_id
            )
            .order_by(ApprovalDecision.created_at.asc())
        ).all()

        detail = {
            "analysis_log_id": log.id,
            "chat_id": log.chat_id,
            "target_message_id": log.target_message_id,
            "status": log.status,
            "created_at": (
                log.created_at.isoformat()
                if log.created_at else None
            ),
            "completed_at": (
                log.completed_at.isoformat()
                if log.completed_at else None
            ),
            "result": result,
            "final_analysis": _final_analysis(result),
            "error_message": log.error_message,
            "decisions": [
                {
                    "id": item.id,
                    "previous_status": item.previous_status,
                    "decision": item.decision,
                    "new_status": item.new_status,
                    "reviewer": item.reviewer,
                    "note": item.note,
                    "created_at": (
                        item.created_at.isoformat()
                        if item.created_at else None
                    ),
                }
                for item in decisions
            ],
        }

        # Aggregator hanya mengambil teks Telegram dan riwayat edit.
        # Memakai Session yang sama; collect_message_bundle tidak
        # menutupnya karena db diberikan.
        detail["source_bundle"] = collect_message_bundle(
            chat_id=detail["chat_id"],
            telegram_message_id=detail["target_message_id"],
            db=db,
        )

        # Manual TP ikut diambil di Session yang sama (sebelumnya
        # checkout koneksi terpisah di send_review_detail).
        detail["manual_tp"] = _get_manual_tp_payload(db, analysis_log_id)

    return detail


def decide_review(
    analysis_log_id,
    decision,
    reviewer,
    note=None,
):
    """
    Mengubah MANUAL_REVIEW menjadi APPROVED atau REJECTED.

    Keputusan dan audit disimpan dalam satu transaksi database.
    Tidak melakukan eksekusi trading.
    """

    if (
        isinstance(analysis_log_id, bool)
        or not isinstance(analysis_log_id, int)
        or analysis_log_id <= 0
    ):
        raise ValueError("Analysis log ID tidak valid.")

    if not isinstance(decision, str):
        raise ValueError("Decision harus berupa teks.")

    decision = decision.strip().upper()

    if decision not in ALLOWED_DECISIONS:
        raise ValueError(
            "Decision hanya boleh APPROVED atau REJECTED."
        )

    if not isinstance(reviewer, str):
        raise ValueError("Nama reviewer harus berupa teks.")

    reviewer = reviewer.strip()

    if not reviewer:
        raise ValueError("Nama reviewer wajib diisi.")

    if len(reviewer) > 100:
        raise ValueError("Nama reviewer maksimal 100 karakter.")

    if note is not None and not isinstance(note, str):
        raise ValueError("Catatan reviewer harus berupa teks.")

    note = note.strip() if note is not None else None

    if note and len(note) > 2000:
        raise ValueError("Catatan maksimal 2000 karakter.")

    if decision == "REJECTED" and not note:
        raise ValueError("Alasan penolakan wajib diisi.")

    with SessionLocal() as db:

        log = db.scalar(
            select(AIAnalysisLog).where(
                AIAnalysisLog.id == analysis_log_id
            )
        )

        if not log:
            raise LookupError("Analisis tidak ditemukan.")

        if log.status != PENDING_STATUS:
            raise ValueError(
                "Keputusan ditolak. Status analisis saat ini: "
                f"{log.status}"
            )

        # UPDATE bersyarat mencegah dua reviewer memutuskan
        # analisis yang sama secara bersamaan.
        result = db.execute(
            update(AIAnalysisLog)
            .where(
                AIAnalysisLog.id == analysis_log_id,
                AIAnalysisLog.status == PENDING_STATUS,
            )
            .values(status=decision)
        )

        if result.rowcount != 1:
            db.rollback()
            raise ValueError(
                "Analisis sudah diputuskan oleh proses lain."
            )

        audit = ApprovalDecision(
            analysis_log_id=analysis_log_id,
            previous_status=PENDING_STATUS,
            decision=decision,
            new_status=decision,
            reviewer=reviewer,
            note=note,
        )

        db.add(audit)

        try:
            db.commit()
        except IntegrityError as error:
            db.rollback()
            raise ValueError(
                "Keputusan sudah tercatat atau terjadi konflik data."
            ) from error

        db.refresh(audit)

        return {
            "analysis_log_id": analysis_log_id,
            "decision_id": audit.id,
            "previous_status": PENDING_STATUS,
            "new_status": decision,
            "reviewer": reviewer,
            "note": note,
            "created_at": (
                audit.created_at.isoformat()
                if audit.created_at else None
            ),
        }

# ============================================================
# RUNTIME CONTROL
# ============================================================

def _ensure_runtime_state(db):
    from app.database.models import RuntimeControlState

    state = db.get(RuntimeControlState, 1)

    if state is None:
        state = RuntimeControlState(
            id=1,
            execution_mode="APPROVAL",
            trading_mode="DRY_RUN",
            auto_execution_enabled=False,
            auto_paused=True,
        )
        db.add(state)
        db.commit()
        db.refresh(state)

    return state


def _runtime_payload(state):
    return {
        "execution_mode": state.execution_mode,
        "trading_mode": state.trading_mode,
        "auto_execution_enabled": state.auto_execution_enabled,
        "auto_paused": state.auto_paused,
        "updated_by": state.updated_by,
        "updated_at": (
            state.updated_at.isoformat()
            if state.updated_at else None
        ),
    }


def get_runtime_control():
    with SessionLocal() as db:
        state = _ensure_runtime_state(db)
        return _runtime_payload(state)


def set_approval_mode(user_id):
    """Mengaktifkan mode approval dan menonaktifkan AUTO."""

    from datetime import datetime, timezone
    from app.database.models import RuntimeControlState

    with SessionLocal() as db:
        state = _ensure_runtime_state(db)

        state.execution_mode = "APPROVAL"
        state.auto_execution_enabled = False
        state.auto_paused = True
        state.updated_by = user_id
        state.updated_at = datetime.now(timezone.utc)

        db.commit()
        db.refresh(state)

        return _runtime_payload(state)


def pause_auto(user_id):
    """Menghentikan penerimaan eksekusi AUTO baru."""

    from datetime import datetime, timezone
    from app.database.models import RuntimeControlState

    with SessionLocal() as db:
        state = _ensure_runtime_state(db)

        state.auto_execution_enabled = False
        state.auto_paused = True
        state.updated_by = user_id
        state.updated_at = datetime.now(timezone.utc)

        db.commit()
        db.refresh(state)

        return _runtime_payload(state)


# ============================================================
# MANUAL TAKE PROFIT
# ============================================================

def _get_manual_tp_payload(db, analysis_log_id):
    """
    Mengambil Manual TP terakhir memakai Session yang diberikan.

    Hasil dimaterialisasi penuh ke dict (atau None). Session tidak
    ditutup di sini; pemanggil yang mengelolanya.
    """
    from app.database.models import ManualTPSetting

    setting = db.scalar(
        select(ManualTPSetting)
        .where(
            ManualTPSetting.analysis_log_id == analysis_log_id
        )
        .order_by(
            ManualTPSetting.created_at.desc(),
            ManualTPSetting.id.desc(),
        )
    )

    if not setting:
        return None

    return {
        "id": setting.id,
        "analysis_log_id": setting.analysis_log_id,
        "symbol": setting.symbol,
        "tp_pips": setting.tp_pips,
        "price_distance": setting.price_distance,
        "status": setting.status,
        "set_by_user_id": setting.set_by_user_id,
        "mt5_position_ticket": setting.mt5_position_ticket,
        "calculated_tp_price": setting.calculated_tp_price,
        "created_at": (
            setting.created_at.isoformat()
            if setting.created_at else None
        ),
    }


def get_manual_tp_setting(analysis_log_id):
    """Wrapper kompatibel: membuka Session sendiri."""
    with SessionLocal() as db:
        return _get_manual_tp_payload(db, analysis_log_id)


def set_manual_tp(analysis_log_id, tp_pips, user_id):
    """
    Menyimpan TP manual dan mempertahankan riwayat perubahan.

    Standar SIREADER:
    1 pip = 0.10 perubahan harga.

    Fungsi ini belum mengubah posisi MT5.
    """

    from decimal import Decimal, InvalidOperation
    from app.database.models import ManualTPSetting

    if (
        isinstance(analysis_log_id, bool)
        or not isinstance(analysis_log_id, int)
        or analysis_log_id <= 0
    ):
        raise ValueError("Analysis ID tidak valid.")

    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
    ):
        raise ValueError("Telegram user ID tidak valid.")

    try:
        pips = Decimal(str(tp_pips).strip().replace(",", "."))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Jumlah TP harus berupa angka.")

    if not pips.is_finite() or pips <= 0:
        raise ValueError("TP harus lebih besar dari nol.")

    if pips > Decimal("100000"):
        raise ValueError("TP melebihi batas input 100.000 pips.")

    if pips != pips.quantize(Decimal("0.01")):
        raise ValueError("TP maksimal memiliki dua angka desimal.")

    price_distance = pips * Decimal("0.10")

    with SessionLocal() as db:
        log = db.scalar(
            select(AIAnalysisLog).where(
                AIAnalysisLog.id == analysis_log_id
            )
        )

        if not log:
            raise LookupError("Analisis tidak ditemukan.")

        if log.status not in {"MANUAL_REVIEW", "APPROVED"}:
            raise ValueError(
                "TP hanya dapat diatur pada analisis "
                "MANUAL_REVIEW atau APPROVED."
            )

        setting = ManualTPSetting(
            analysis_log_id=analysis_log_id,
            symbol="XAUUSD",
            tp_pips=pips,
            price_distance=price_distance,
            set_by_user_id=user_id,
            status="WAITING_FOR_EXECUTION",
        )

        db.add(setting)
        db.commit()
        db.refresh(setting)

        return {
            "id": setting.id,
            "analysis_log_id": setting.analysis_log_id,
            "symbol": setting.symbol,
            "tp_pips": setting.tp_pips,
            "price_distance": setting.price_distance,
            "status": setting.status,
        }


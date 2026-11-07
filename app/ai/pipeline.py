
import json
from datetime import datetime, timezone
from app.signals.trade_episode_repository import (
    persist_bundle_trade_episodes,
)

from app.signals.decision_service import (
    evaluate_pipeline_decision,
    map_decision_to_analysis_status,
)
from app.signals.trade_decision_watch_repository import (
    create_watch as create_trade_decision_watch,
)
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import AIAnalysisLog, TelegramOutbox

from app.ai.message_aggregator import collect_message_bundle
from app.ai.ai_router import analyze_bundle
from app.ai.pending_queue import (
    BUDGET_BLOCK_STATUSES,
    enqueue_pending_message,
)
from app.ai.cost_guard import (
    get_analysis_usage_status,
    reset_analysis_context,
    set_analysis_context,
)


CHECKPOINT_TYPE = "SIREADER_CHECKPOINT"


def get_existing_analysis(chat_id, target_message_id):
    """Memeriksa apakah pesan sudah pernah dianalisis."""

    with SessionLocal() as db:
        existing = db.scalar(
            select(AIAnalysisLog).where(
                AIAnalysisLog.chat_id == chat_id,
                AIAnalysisLog.target_message_id == target_message_id,
            )
        )

        if not existing:
            return None

        return {
            "id": existing.id,
            "status": existing.status,
            "result_json": existing.result_json,
        }


def save_analysis_checkpoint(log_id, resume_state):
    """
    Menyimpan checkpoint dan commit sebelum tier berikutnya berjalan.
    """

    if not isinstance(resume_state, dict):
        raise ValueError("Resume state checkpoint tidak valid.")

    payload = {
        "_type": CHECKPOINT_TYPE,
        "resume_state": resume_state,
    }

    with SessionLocal() as db:
        log = db.scalar(
            select(AIAnalysisLog).where(
                AIAnalysisLog.id == log_id
            )
        )

        if not log:
            raise ValueError("AIAnalysisLog tidak ditemukan.")

        if log.status != "PROCESSING":
            raise ValueError(
                "Checkpoint hanya boleh disimpan saat PROCESSING."
            )

        log.result_json = json.dumps(
            payload,
            ensure_ascii=False,
            default=str,
        )

        db.commit()


def load_analysis_checkpoint(result_json):
    """Mengambil resume_state dari checkpoint yang tersimpan."""

    if not result_json:
        return None

    try:
        payload = json.loads(result_json)
    except (TypeError, ValueError):
        return None

    if not isinstance(payload, dict):
        return None

    if payload.get("_type") != CHECKPOINT_TYPE:
        return None

    state = payload.get("resume_state")

    if not isinstance(state, dict):
        return None

    return state


def check_retry_safety(log_id, result_json):
    """
    Memastikan retry tidak mengulang transaksi AI yang sudah dibayar.

    RESERVED dan UNKNOWN selalu memerlukan pemeriksaan manual.
    COMPLETED harus cocok dengan request_id di checkpoint.
    """

    usage = get_analysis_usage_status(log_id)
    transactions = usage.get("transactions") or []

    uncertain = [
        item
        for item in transactions
        if item.get("status") in ("RESERVED", "UNKNOWN")
    ]

    if uncertain or usage.get("has_uncertain"):
        return {
            "safe": False,
            "reason": "UNCERTAIN_BILLING_TRANSACTION",
        }

    completed = [
        item
        for item in transactions
        if item.get("status") == "COMPLETED"
    ]

    checkpoint = load_analysis_checkpoint(result_json)

    # Tidak ada transaksi berhasil. Retry atau mulai dari awal aman.
    if not completed:
        return {
            "safe": True,
            "resume_state": checkpoint,
        }

    # Ada transaksi berhasil tetapi tidak ada checkpoint.
    if checkpoint is None:
        return {
            "safe": False,
            "reason": "COMPLETED_TRANSACTION_WITHOUT_CHECKPOINT",
        }

    attempts = checkpoint.get("attempts") or []

    if not isinstance(attempts, list):
        return {
            "safe": False,
            "reason": "INVALID_CHECKPOINT_ATTEMPTS",
        }

    completed_ids = {
        item.get("request_id")
        for item in completed
        if isinstance(item.get("request_id"), str)
        and item.get("request_id")
    }

    checkpoint_ids = {
        item.get("request_id")
        for item in attempts
        if isinstance(item, dict)
        and isinstance(item.get("request_id"), str)
        and item.get("request_id")
    }

    # Jumlah ID valid harus sama dengan jumlah transaksi COMPLETED.
    # Semua transaksi COMPLETED harus tercatat di checkpoint.
    if (
        len(completed_ids) != len(completed)
        or completed_ids != checkpoint_ids
    ):
        return {
            "safe": False,
            "reason": "CHECKPOINT_BILLING_MISMATCH",
        }

    return {
        "safe": True,
        "resume_state": checkpoint,
    }


def create_analysis_log(chat_id, target_message_id):
    """
    Membuat catatan PROCESSING sebelum AI dipanggil.

    Unique constraint mencegah analisis ganda
    untuk target pesan yang sama.
    """

    with SessionLocal() as db:
        log = AIAnalysisLog(
            chat_id=chat_id,
            target_message_id=target_message_id,
            status="PROCESSING",
        )

        db.add(log)

        try:
            db.commit()
            db.refresh(log)

            return log.id

        except IntegrityError:
            db.rollback()

            existing = db.scalar(
                select(AIAnalysisLog).where(
                    AIAnalysisLog.chat_id == chat_id,
                    AIAnalysisLog.target_message_id == target_message_id,
                )
            )

            if existing:
                return None

            raise


def claim_failed_analysis(chat_id, target_message_id):
    """
    Mengambil alih satu analisis FAILED untuk retry manual.

    UPDATE bersyarat memastikan hanya satu proses
    yang dapat mengklaim retry secara bersamaan.
    """

    with SessionLocal() as db:
        result = db.execute(
            update(AIAnalysisLog)
            .where(
                AIAnalysisLog.chat_id == chat_id,
                AIAnalysisLog.target_message_id == target_message_id,
                AIAnalysisLog.status == "FAILED",
            )
            .values(
                status="PROCESSING",
                completed_at=None,
            )
        )

        db.commit()

        return result.rowcount == 1


def _build_trade_decision_watch_payload(decision):
    """
    Build a validated repository payload only for WAITING_FOR_SPREAD.
    Episode ID from Decision Service is an episode_key string.
    """
    if not isinstance(decision, dict):
        return None

    if decision.get("status") != "WAITING_FOR_SPREAD":
        return None

    market_data = decision.get("market_data")

    if not isinstance(market_data, dict):
        raise ValueError(
            "WAITING_FOR_SPREAD tidak memiliki market_data."
        )

    if market_data.get("available") is not True:
        raise ValueError(
            "WAITING_FOR_SPREAD tidak memiliki market data yang valid."
        )

    episode_key = decision.get("trade_episode_id")

    if not isinstance(episode_key, str) or not episode_key.strip():
        raise ValueError(
            "WAITING_FOR_SPREAD tidak memiliki trade_episode_key."
        )

    symbol = market_data.get("symbol")
    bid = market_data.get("bid")
    ask = market_data.get("ask")
    maximum_spread_pips = decision.get("maximum_spread_pips")

    if not symbol:
        raise ValueError(
            "Symbol MT5 tidak tersedia untuk spread watch."
        )

    if bid is None or ask is None:
        raise ValueError(
            "Bid/Ask tidak tersedia untuk spread watch."
        )

    if maximum_spread_pips is None:
        raise ValueError(
            "Batas maximum_spread_pips tidak tersedia dari Decision Engine."
        )

    return {
        "trade_episode_key": episode_key.strip(),
        "symbol": symbol,
        "maximum_spread_pips": maximum_spread_pips,
        "bid": bid,
        "ask": ask,
        "broker_server": market_data.get("broker_server"),
    }


def finish_analysis_log(
    log_id,
    status,
    result=None,
    error_message=None,
    decision_watch=None,
):
    """
    Menyimpan hasil analisis dan antrean notifikasi
    dalam satu transaksi database.
    """

    with SessionLocal() as db:
        log = db.scalar(
            select(AIAnalysisLog).where(
                AIAnalysisLog.id == log_id
            )
        )

        if not log:
            raise ValueError(
                "AIAnalysisLog tidak ditemukan."
            )

        log.status = status

        if result is not None:
            log.result_json = json.dumps(
                result,
                ensure_ascii=False,
                default=str,
            )

        if error_message:
            new_error = str(error_message)[:1000]
            previous_error = log.error_message

            if previous_error:
                combined_error = (
                    f"{previous_error}\n"
                    f"[RETRY] {new_error}"
                )
                log.error_message = combined_error[-1000:]
            else:
                log.error_message = new_error

        log.completed_at = datetime.now(timezone.utc)

        # Pesan yang terblokir budget masuk antrean terpisah.
        if status in BUDGET_BLOCK_STATUSES:
            enqueue_pending_message(
                db,
                chat_id=log.chat_id,
                telegram_message_id=log.target_message_id,
                analysis_log_id=log.id,
                reason=status,
            )

        # Notifikasi manual review dibuat dalam transaksi yang sama.
        if status == "MANUAL_REVIEW":
            recipient = settings.TELEGRAM_APPROVER_USER_ID

            if recipient is not None and recipient > 0:
                event_key = f"MANUAL_REVIEW:{log.id}"

                existing = db.scalar(
                    select(TelegramOutbox).where(
                        TelegramOutbox.event_key == event_key
                    )
                )

                if not existing:
                    db.add(
                        TelegramOutbox(
                            event_key=event_key,
                            analysis_log_id=log.id,
                            recipient_chat_id=recipient,
                            event_type="MANUAL_REVIEW",
                            status="PENDING",
                        )
                    )

        # Watch dibuat dalam transaksi yang sama dengan AIAnalysisLog.
        # Tidak ada approval notification untuk status ANALYZED.
        if decision_watch is not None:
            if status != "ANALYZED":
                raise ValueError(
                    "Spread watch hanya boleh disimpan bersama status ANALYZED."
                )

            create_trade_decision_watch(
                db,
                analysis_log_id=log.id,
                **decision_watch,
            )

        db.commit()


def process_telegram_message(
    chat_id,
    telegram_message_id,
    retry_failed=False,
):
    """
    Pipeline utama SIREADER.

    1. Deduplikasi pesan.
    2. Pemeriksaan keamanan retry.
    3. Pengumpulan konteks teks.
    4. Pembuatan log atau klaim FAILED.
    5. Analisis dengan checkpoint dan resume.
    6. Penyimpanan hasil akhir.

    Fungsi ini tidak melakukan eksekusi trading.
    """

    # --------------------------------------------------------
    # 1. DEDUPLICATION DAN PEMERIKSAAN RETRY
    # --------------------------------------------------------

    existing = get_existing_analysis(
        chat_id,
        telegram_message_id,
    )

    retrying = False
    resume_state = None

    if existing:
        if (
            retry_failed is True
            and existing["status"] == "FAILED"
        ):
            retrying = True

            try:
                safety = check_retry_safety(
                    existing["id"],
                    existing.get("result_json"),
                )

            except Exception as error:
                finish_analysis_log(
                    log_id=existing["id"],
                    status="MANUAL_REVIEW",
                    error_message=(
                        "Pemeriksaan keamanan retry gagal: "
                        f"{error}"
                    ),
                )

                return {
                    "status": "MANUAL_REVIEW",
                    "analysis_log_id": existing["id"],
                    "reason": "RETRY_SAFETY_CHECK_FAILED",
                    "retried": False,
                }

            if not safety["safe"]:
                finish_analysis_log(
                    log_id=existing["id"],
                    status="MANUAL_REVIEW",
                    error_message=(
                        "Retry diblokir: "
                        f"{safety['reason']}"
                    ),
                )

                return {
                    "status": "MANUAL_REVIEW",
                    "analysis_log_id": existing["id"],
                    "reason": safety["reason"],
                    "retried": False,
                }

            resume_state = safety.get("resume_state")

        else:
            return {
                "status": "SKIPPED_DUPLICATE",
                "analysis_log_id": existing["id"],
                "existing_status": existing["status"],
            }

    # --------------------------------------------------------
    # 2. KUMPULKAN KONTEKS TEKS
    # --------------------------------------------------------

    bundle = collect_message_bundle(
        chat_id,
        telegram_message_id,
    )

    if not bundle:
        return {
            "status": "SKIPPED_NO_TEXT",
        }

    messages = bundle.get("messages") or []

    if not any(
        isinstance(message.get("text"), str)
        and message["text"].strip()
        for message in messages
    ):
        return {
            "status": "SKIPPED_NO_TEXT",
        }

    # --------------------------------------------------------
    # 3. BUAT LOG BARU ATAU KLAIM FAILED
    # --------------------------------------------------------

    if retrying:
        claimed = claim_failed_analysis(
            chat_id,
            telegram_message_id,
        )

        if not claimed:
            latest = get_existing_analysis(
                chat_id,
                telegram_message_id,
            )

            return {
                "status": "SKIPPED_DUPLICATE",
                "analysis_log_id": (
                    latest["id"] if latest else None
                ),
                "existing_status": (
                    latest["status"] if latest else None
                ),
            }

        log_id = existing["id"]

    else:
        log_id = create_analysis_log(
            chat_id,
            telegram_message_id,
        )

        if log_id is None:
            latest = get_existing_analysis(
                chat_id,
                telegram_message_id,
            )

            return {
                "status": "SKIPPED_DUPLICATE",
                "analysis_log_id": (
                    latest["id"] if latest else None
                ),
                "existing_status": (
                    latest["status"] if latest else None
                ),
            }
    # --------------------------------------------------------
    # 3B. PERSISTENT TRADE EPISODE SYNCHRONIZATION
    # --------------------------------------------------------

    has_classified_messages = any(
        isinstance(message, dict)
        and isinstance(message.get("message_roles"), list)
        for message in messages
    )

    if has_classified_messages:
        try:
            with SessionLocal() as episode_db:
                persist_bundle_trade_episodes(
                    episode_db,
                    bundle,
                )

                episode_db.commit()

        except Exception as exc:
            finish_analysis_log(
                log_id=log_id,
                status="MANUAL_REVIEW",
                error_message=(
                    "Persistent trade episode synchronization "
                    f"failed: {type(exc).__name__}"
                ),
            )

            return {
                "status": "MANUAL_REVIEW",
                "analysis_log_id": log_id,
                "reason": "TRADE_EPISODE_PERSISTENCE_FAILED",
                "retried": retrying,
            }

    # --------------------------------------------------------
    # 4. CALLBACK CHECKPOINT
    # --------------------------------------------------------

    def checkpoint_callback(state):
        save_analysis_checkpoint(
            log_id,
            state,
        )

    # --------------------------------------------------------
    # 5. ANALISIS MELALUI AI ROUTER
    # --------------------------------------------------------

    try:
        token = set_analysis_context(log_id)

        try:
            result = analyze_bundle(
                bundle,
                resume_state=resume_state,
                checkpoint_callback=checkpoint_callback,
            )
        finally:
            reset_analysis_context(token)

        if not isinstance(result, dict):
            raise ValueError(
                "AI Router mengembalikan hasil tidak valid."
            )

        final_status = result.get("status")
        decision_watch_data = None

        if not isinstance(final_status, str):
            raise ValueError(
                "Status hasil AI tidak valid."
            )

        # --------------------------------------------------------
        # 5B. DECISION SERVICE
        # --------------------------------------------------------

        if final_status == "ANALYZED":
            try:
                decision = evaluate_pipeline_decision(
                    result,
                    bundle=bundle,
                    analysis_log_id=log_id,
                    session_factory=SessionLocal,
                )

                if not isinstance(decision, dict):
                    raise ValueError(
                        "Decision Service mengembalikan hasil tidak valid."
                    )

                mapped_status = map_decision_to_analysis_status(
                    decision
                )

                if not isinstance(mapped_status, str):
                    raise ValueError(
                        "Pemetaan status keputusan tidak valid."
                    )

                # Pertahankan seluruh hasil AI Router.
                # Tambahkan keputusan sebagai bagian terpisah.
                result = dict(result)
                result["decision"] = decision

                decision_watch_data = (
                    _build_trade_decision_watch_payload(decision)
                )

                final_status = mapped_status

            except Exception as decision_error:
                # Fail-closed:
                # kegagalan Decision Service tidak boleh dianggap siap.
                result = dict(result)
                result["decision"] = {
                    "status": "NEEDS_REVIEW",
                    "reason": "DECISION_SERVICE_FAILED",
                    "error": (
                        f"{type(decision_error).__name__}: "
                        f"{decision_error}"
                    )[:500],
                    "order_sent": False,
                }

                final_status = "MANUAL_REVIEW"

    except Exception as error:
        finish_analysis_log(
            log_id=log_id,
            status="FAILED",
            error_message=str(error),
        )

        return {
            "status": "FAILED",
            "analysis_log_id": log_id,
            "error": str(error),
            "retried": retrying,	
        }

    # --------------------------------------------------------
    # 6. SIMPAN HASIL AKHIR
    # --------------------------------------------------------

    if decision_watch_data is None:
        finish_analysis_log(
            log_id=log_id,
            status=final_status,
            result=result,
        )
    else:
        try:
            finish_analysis_log(
                log_id=log_id,
                status=final_status,
                result=result,
                decision_watch=decision_watch_data,
            )

        except Exception as watch_error:
            # Fail-closed: jangan biarkan analisis terlihat berhasil
            # menunggu spread jika watch gagal tersimpan.
            result = dict(result or {})
            failed_decision = result.get("decision")

            if not isinstance(failed_decision, dict):
                failed_decision = {}

            failed_decision = dict(failed_decision)
            failed_decision.update({
                "status": "NEEDS_REVIEW",
                "reason": "TRADE_DECISION_WATCH_PERSISTENCE_FAILED",
                "error": (
                    f"{type(watch_error).__name__}: {watch_error}"
                )[:500],
                "order_sent": False,
            })

            result["decision"] = failed_decision
            final_status = "MANUAL_REVIEW"

            finish_analysis_log(
                log_id=log_id,
                status=final_status,
                result=result,
                error_message=(
                    "Trade decision watch persistence failed: "
                    f"{type(watch_error).__name__}"
                ),
            )

    return {
        "status": final_status,
        "analysis_log_id": log_id,
        "analysis": result,
        "retried": retrying,
    }

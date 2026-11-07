"""Read-only MT5 spread decision watch worker for SIREADER.

The worker never sends, modifies, or closes MT5 orders. It only reads DEMO
quotes, re-evaluates a stored AI decision, and updates database workflow state.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.ai.message_aggregator import collect_message_bundle
from app.config import settings
from app.database.connection import SessionLocal
from app.database.models import (
    AIAnalysisLog,
    TelegramOutbox,
    TradeDecisionWatch,
    TradeEpisode,
)
from app.execution import mt5_connector
from app.signals import trade_decision_watch_repository as repository
from app.signals.decision_service import (
    evaluate_pipeline_decision,
    map_decision_to_analysis_status,
)

logger = logging.getLogger(__name__)

WATCH_POLL_SECONDS = 1.0
WATCH_RETRY_SECONDS = 5
WATCH_LEASE_SECONDS = 60
WATCH_BATCH_LIMIT = 50
WORKER_TASK_KEY = "trade_decision_watch_worker_task"


class WatchWorkerError(RuntimeError):
    """Raised for invalid or unavailable watch-worker inputs."""


def _utcnow():
    return datetime.now(timezone.utc)


def _get_approver_user_id():
    value = getattr(settings, "TELEGRAM_APPROVER_USER_ID", None)
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return value if value > 0 else 0


def read_mt5_quote(symbol, broker_server=None):
    """Read one quote from the configured terminal, requiring a DEMO account."""
    terminal_path = getattr(settings, "MT5_TERMINAL_PATH", None)
    configured_server = getattr(settings, "MT5_EXPECTED_SERVER", None)

    if not terminal_path:
        raise WatchWorkerError("MT5_TERMINAL_PATH_NOT_CONFIGURED")

    if (
        broker_server
        and configured_server
        and str(broker_server).strip() != str(configured_server).strip()
    ):
        raise WatchWorkerError("WATCH_BROKER_SERVER_CONFIGURATION_MISMATCH")

    expected_server = broker_server or configured_server or None
    connected = False

    try:
        connection = mt5_connector.connect_readonly(
            terminal_path=terminal_path,
            expected_server=expected_server,
            require_demo=True,
        )
        connected = True

        account = connection.get("account") or {}
        if account.get("mode") != "DEMO":
            raise WatchWorkerError("MT5_ACCOUNT_IS_NOT_DEMO")

        spec = mt5_connector.get_market_clock_snapshot(symbol)
        if not isinstance(spec, dict):
            raise WatchWorkerError("MT5_SYMBOL_SPEC_INVALID")

        if str(spec.get("symbol", "")).strip() != str(symbol).strip():
            raise WatchWorkerError("MT5_SYMBOL_MISMATCH")

        if spec.get("bid") is None or spec.get("ask") is None:
            raise WatchWorkerError("MT5_QUOTE_MISSING")

        return {
            "ok": True,
            "symbol": spec["symbol"],
            "bid": spec["bid"],
            "ask": spec["ask"],
            "tick_size": spec.get("tick_size"),
            "tick_time": spec.get("tick_time"),
            "tick_time_msc": spec.get("tick_time_msc"),
            "reference_time_msc": spec.get("reference_time_msc"),
            "reference_symbol": spec.get("reference_symbol"),
            "reference_clock_confirmed": spec.get("reference_clock_confirmed"),
            "reference_clock_reason": spec.get("reference_clock_reason"),
            "reference_silence_seconds": spec.get("reference_silence_seconds"),
            "clock_source": spec.get("clock_source"),
            "account_mode": "DEMO",
            "readonly": True,
            "broker_server": account.get("server") or expected_server,
        }
    finally:
        if connected:
            try:
                mt5_connector.shutdown()
            except Exception:
                logger.exception("MT5 shutdown gagal setelah quote read.")


def _queue_manual_review(db, log):
    recipient = _get_approver_user_id()
    if recipient <= 0:
        return False

    event_key = f"MANUAL_REVIEW:{log.id}"
    existing = db.scalar(
        select(TelegramOutbox).where(
            TelegramOutbox.event_key == event_key
        )
    )
    if existing is not None:
        return False

    db.add(
        TelegramOutbox(
            event_key=event_key,
            analysis_log_id=log.id,
            recipient_chat_id=recipient,
            event_type="MANUAL_REVIEW",
            status="PENDING",
        )
    )
    return True


def _reschedule(
    watch_id,
    lease_until,
    *,
    reason,
    session_factory,
    now,
    retry_seconds=WATCH_RETRY_SECONDS,
):
    with session_factory() as db:
        try:
            repository.reschedule_claimed_watch(
                db,
                watch_id,
                lease_until=lease_until,
                next_check_at=now + timedelta(seconds=retry_seconds),
                reason=reason,
                now=now,
            )
            db.commit()
            return {
                "watch_id": watch_id,
                "status": "RETRY_SCHEDULED",
                "reason": reason,
            }
        except repository.TradeDecisionWatchConflictError:
            db.rollback()
            return {
                "watch_id": watch_id,
                "status": "LEASE_LOST",
            }


def _finish_invalid_record(
    watch_id,
    lease_until,
    *,
    session_factory,
    now,
    reason,
):
    """Fail closed when the persistent analysis payload cannot be trusted."""
    with session_factory() as db:
        watch = db.get(TradeDecisionWatch, watch_id)
        log = db.get(AIAnalysisLog, watch.analysis_log_id) if watch else None
        if watch is None or log is None:
            db.rollback()
            return {"watch_id": watch_id, "status": "RECORD_MISSING"}

        try:
            repository.finish_claimed_watch(
                db,
                watch_id,
                lease_until=lease_until,
                target_status=repository.WATCH_ERROR,
                reason=reason,
                now=now,
            )
            fallback = {
                "status": "ANALYZED",
                "final_analysis": {},
                "attempts": [],
                "decision": {
                    "status": "NEEDS_REVIEW",
                    "reason": "TRADE_DECISION_WATCH_CONTEXT_INVALID",
                    "detail": reason[:300],
                    "order_sent": False,
                },
            }
            log.status = "MANUAL_REVIEW"
            log.result_json = json.dumps(
                fallback,
                ensure_ascii=False,
                default=str,
            )
            log.error_message = reason[:1000]
            log.completed_at = now
            _queue_manual_review(db, log)
            db.commit()
            return {"watch_id": watch_id, "status": "ERROR_REVIEW"}
        except Exception:
            db.rollback()
            logger.exception("Gagal menyelesaikan watch dengan record invalid.")
            return {"watch_id": watch_id, "status": "PERSISTENCE_ERROR"}


def _finish_episode_not_open(
    watch_id,
    lease_until,
    *,
    session_factory,
    now,
    episode_status,
):
    with session_factory() as db:
        watch = db.get(TradeDecisionWatch, watch_id)
        log = db.get(AIAnalysisLog, watch.analysis_log_id) if watch else None
        if watch is None or log is None:
            db.rollback()
            return {"watch_id": watch_id, "status": "RECORD_MISSING"}

        try:
            repository.finish_claimed_watch(
                db,
                watch_id,
                lease_until=lease_until,
                target_status=repository.WATCH_SKIPPED,
                reason=f"EPISODE_NOT_OPEN:{episode_status}",
                now=now,
            )
            try:
                result = json.loads(log.result_json or "{}")
            except (TypeError, ValueError):
                result = {}
            if not isinstance(result, dict):
                result = {}
            result["decision"] = {
                "status": "SKIPPED",
                "reason": f"EPISODE_NOT_OPEN:{episode_status}",
                "order_sent": False,
            }
            log.result_json = json.dumps(
                result,
                ensure_ascii=False,
                default=str,
            )
            # Mentor skip / episode closed cancels this watch without approval.
            log.status = "ANALYZED"
            log.completed_at = now
            db.commit()
            return {
                "watch_id": watch_id,
                "status": "SKIPPED",
                "reason": f"EPISODE_NOT_OPEN:{episode_status}",
            }
        except Exception:
            db.rollback()
            logger.exception("Gagal membatalkan watch untuk episode non-OPEN.")
            return {"watch_id": watch_id, "status": "PERSISTENCE_ERROR"}


def process_watch(
    watch_id,
    *,
    session_factory=None,
    quote_reader=None,
    bundle_loader=None,
    decision_evaluator=None,
    status_mapper=None,
    now=None,
    lease_seconds=WATCH_LEASE_SECONDS,
    retry_seconds=WATCH_RETRY_SECONDS,
    spread_retry_seconds=1,
):
    """Claim and process one watch. No trading order can be sent here."""
    session_factory = session_factory or SessionLocal
    quote_reader = quote_reader or read_mt5_quote
    bundle_loader = bundle_loader or collect_message_bundle
    decision_evaluator = decision_evaluator or evaluate_pipeline_decision
    status_mapper = status_mapper or map_decision_to_analysis_status
    fixed_clock = now is not None
    now = now or _utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # Claim atomically; next_check_at acts as a temporary lease.
    with session_factory() as db:
        claimed = repository.claim_due_watch(
            db,
            watch_id,
            now=now,
            lease_seconds=lease_seconds,
        )
        if claimed is None:
            db.rollback()
            return {"watch_id": watch_id, "status": "NOT_CLAIMED"}

        watch, lease_until = claimed
        log = db.get(AIAnalysisLog, watch.analysis_log_id)
        episode = db.get(TradeEpisode, watch.trade_episode_id)

        if log is None or episode is None:
            db.commit()
            return _finish_invalid_record(
                watch_id,
                lease_until,
                session_factory=session_factory,
                now=now,
                reason="WATCH_PARENT_RECORD_MISSING",
            )

        snapshot = {
            "analysis_log_id": log.id,
            "chat_id": log.chat_id,
            "target_message_id": log.target_message_id,
            "result_json": log.result_json,
            "episode_status": episode.status,
            "symbol": watch.symbol,
            "broker_server": watch.broker_server,
        }
        db.commit()

    if snapshot["episode_status"] != "OPEN":
        return _finish_episode_not_open(
            watch_id,
            lease_until,
            session_factory=session_factory,
            now=now,
            episode_status=snapshot["episode_status"],
        )

    try:
        stored_result = json.loads(snapshot["result_json"] or "")
        if not isinstance(stored_result, dict):
            raise ValueError("AI_ANALYSIS_RESULT_NOT_OBJECT")
        ai_result = dict(stored_result)
        ai_result.pop("decision", None)
        if ai_result.get("status") != "ANALYZED":
            raise ValueError("AI_ANALYSIS_RESULT_NOT_ANALYZED")
        final_analysis = ai_result.get("final_analysis")
        if not isinstance(final_analysis, dict):
            raise ValueError("AI_FINAL_ANALYSIS_MISSING")
    except Exception as error:
        return _finish_invalid_record(
            watch_id,
            lease_until,
            session_factory=session_factory,
            now=now,
            reason=f"INVALID_STORED_AI_RESULT:{type(error).__name__}",
        )

    try:
        quote = quote_reader(
            snapshot["symbol"],
            snapshot["broker_server"],
        )
        if not isinstance(quote, dict) or quote.get("ok") is not True:
            reason = (
                quote.get("reason", "MT5_QUOTE_UNAVAILABLE")
                if isinstance(quote, dict)
                else "MT5_QUOTE_UNAVAILABLE"
            )
            return _reschedule(
                watch_id,
                lease_until,
                reason=f"MT5_READ_RETRY:{str(reason)[:160]}",
                session_factory=session_factory,
                now=now if fixed_clock else _utcnow(),
                retry_seconds=retry_seconds,
            )

        if str(quote.get("symbol", "")).strip() != snapshot["symbol"]:
            raise WatchWorkerError("MT5_SYMBOL_MISMATCH")
        if quote.get("account_mode") != "DEMO" or quote.get("readonly") is not True:
            raise WatchWorkerError("MT5_READONLY_DEMO_REQUIRED")

        bundle = bundle_loader(
            snapshot["chat_id"],
            snapshot["target_message_id"],
        )
        if not isinstance(bundle, dict) or not bundle.get("messages"):
            return _reschedule(
                watch_id,
                lease_until,
                reason="BUNDLE_RELOAD_RETRY",
                session_factory=session_factory,
                now=now if fixed_clock else _utcnow(),
                retry_seconds=retry_seconds,
            )

        evaluation_now = now if fixed_clock else _utcnow()
        decision = decision_evaluator(
            ai_result,
            bundle=bundle,
            analysis_log_id=snapshot["analysis_log_id"],
            session_factory=session_factory,
            market_data=quote,
            now=evaluation_now,
        )
        if not isinstance(decision, dict):
            raise WatchWorkerError("DECISION_RESULT_INVALID")
    except Exception as error:
        logger.exception("Trade decision watch gagal diproses: %s", watch_id)
        return _reschedule(
            watch_id,
            lease_until,
            reason=f"RECHECK_RETRY:{type(error).__name__}",
            session_factory=session_factory,
            now=_utcnow() if now is None else now,
            retry_seconds=retry_seconds,
        )

    market_metadata = decision.get("market_data")
    if isinstance(market_metadata, dict) and market_metadata.get("available") is False:
        reason = market_metadata.get("reason") or "MT5_MARKET_DATA_NOT_FRESH"
        return _reschedule(
            watch_id,
            lease_until,
            reason=f"MT5_FRESHNESS_RETRY:{str(reason)[:160]}",
            session_factory=session_factory,
            now=_utcnow() if now is None else now,
            retry_seconds=retry_seconds,
        )

    decision = dict(decision)
    # A hard safety invariant: this worker never sends an order.
    decision["order_sent"] = False
    decision_status = decision.get("status")

    if decision_status == "WAITING_FOR_SPREAD":
        bid = quote.get("bid")
        ask = quote.get("ask")
        if bid is None or ask is None:
            return _reschedule(
                watch_id,
                lease_until,
                reason="WAITING_SPREAD_WITHOUT_BID_ASK",
                session_factory=session_factory,
                now=now if fixed_clock else _utcnow(),
                retry_seconds=retry_seconds,
            )

        checked_at = now if fixed_clock else _utcnow()
        with session_factory() as db:
            try:
                watch = repository.update_claimed_watch_check(
                    db,
                    watch_id,
                    lease_until=lease_until,
                    bid=bid,
                    ask=ask,
                    checked_at=checked_at,
                    retry_seconds=spread_retry_seconds,
                )
                db.commit()
                return {
                    "watch_id": watch_id,
                    "status": watch.status,
                    "spread_pips": str(watch.spread_pips),
                    "reason": watch.last_reason,
                }
            except repository.TradeDecisionWatchConflictError:
                db.rollback()
                return {"watch_id": watch_id, "status": "LEASE_LOST"}

    try:
        mapped_status = status_mapper(decision)
        if not isinstance(mapped_status, str):
            raise ValueError("DECISION_STATUS_MAPPING_INVALID")
    except Exception:
        logger.exception("Mapping status watch gagal: %s", watch_id)
        mapped_status = "MANUAL_REVIEW"
        decision["status"] = "NEEDS_REVIEW"
        decision["reason"] = "WATCH_DECISION_STATUS_MAPPING_FAILED"
        decision["order_sent"] = False

    target_watch_status = (
        repository.WATCH_SKIPPED
        if decision_status in {"SKIP", "SKIPPED"}
        else repository.WATCH_RESOLVED
    )
    finish_time = now if fixed_clock else _utcnow()

    with session_factory() as db:
        log = db.get(AIAnalysisLog, snapshot["analysis_log_id"])
        if log is None:
            db.rollback()
            return {"watch_id": watch_id, "status": "ANALYSIS_LOG_MISSING"}

        try:
            repository.finish_claimed_watch(
                db,
                watch_id,
                lease_until=lease_until,
                target_status=target_watch_status,
                reason=f"RECHECKED:{decision.get('status', 'UNKNOWN')}",
                now=finish_time,
            )

            try:
                saved_result = json.loads(log.result_json or "{}")
            except (TypeError, ValueError):
                saved_result = {}
            if not isinstance(saved_result, dict):
                saved_result = {}
            saved_result["decision"] = decision

            log.status = mapped_status
            log.result_json = json.dumps(
                saved_result,
                ensure_ascii=False,
                default=str,
            )
            log.completed_at = finish_time

            if mapped_status == "MANUAL_REVIEW":
                _queue_manual_review(db, log)

            db.commit()
            return {
                "watch_id": watch_id,
                "status": target_watch_status,
                "decision_status": decision.get("status"),
                "analysis_status": mapped_status,
            }
        except repository.TradeDecisionWatchConflictError:
            db.rollback()
            return {"watch_id": watch_id, "status": "LEASE_LOST"}
        except Exception:
            db.rollback()
            logger.exception("Finalisasi trade decision watch gagal: %s", watch_id)
            return {"watch_id": watch_id, "status": "PERSISTENCE_ERROR"}


def process_due_watches(
    *,
    session_factory=None,
    limit=WATCH_BATCH_LIMIT,
    **process_kwargs,
):
    """Process due watch IDs; per-watch atomic claim prevents duplicate work."""
    session_factory = session_factory or SessionLocal
    now = process_kwargs.get("now") or _utcnow()
    with session_factory() as db:
        due_ids = [
            item.id
            for item in repository.get_due_watches(
                db,
                now=now,
                limit=limit,
            )
        ]

    return [
        process_watch(
            watch_id,
            session_factory=session_factory,
            **process_kwargs,
        )
        for watch_id in due_ids
    ]


async def watch_worker():
    """Async polling loop; does database/MT5 work in a worker thread."""
    logger.info("Trade decision watch worker aktif.")
    while True:
        try:
            await asyncio.to_thread(process_due_watches)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Trade decision watch cycle gagal.")
        await asyncio.sleep(WATCH_POLL_SECONDS)


def start_watch_worker(application):
    """Start exactly one watch worker task on an application bot_data."""
    existing = application.bot_data.get(WORKER_TASK_KEY)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(
        watch_worker(),
        name="sireader-trade-decision-watch",
    )
    application.bot_data[WORKER_TASK_KEY] = task
    logger.info("Trade decision watch worker dimulai.")
    return task


async def stop_watch_worker(application):
    """Stop the watch worker without creating a replacement task."""
    task = application.bot_data.pop(WORKER_TASK_KEY, None)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    logger.info("Trade decision watch worker dihentikan.")

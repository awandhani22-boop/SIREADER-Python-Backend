"""Safe orchestration layer between AI Router, episode context and Decision Engine.

This module never sends an MT5 order. MT5 is used only for fresh demo-account
market data through the existing read-only connector.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from app.database.models import ManualTPSetting, RuntimeControlState
from app.signals.decision_engine import PIP_PRICE, evaluate_signal


SIGNAL_CONTEXT_ROLES = {
    "NEW_SIGNAL",
    "ENTRY_REQUEST",
    "PRICE_ZONE",
    "SETUP_HINT",
}

DECISION_REVIEW_STATUSES = {
    "READY_FOR_APPROVAL",
    "NEEDS_REVIEW",
    "SKIP",
    "AUTO_PAUSED",
}

WAITING_DECISION_STATUSES = {
    "WAITING_FOR_ZONE",
    "WAITING_FOR_CISD",
    "WAITING_FOR_SPREAD",
}


def _roles(message):
    values = message.get("message_roles") or []
    if not isinstance(values, list):
        values = []

    single = message.get("message_role")
    if isinstance(single, str):
        values = values + [single]

    return {value for value in values if isinstance(value, str)}


def _resolve_episode_id(bundle):
    """Return the one source episode ID, or None when absent/ambiguous."""
    if not isinstance(bundle, dict):
        return None, "BUNDLE_INVALID"

    candidates = set()
    top_level = bundle.get("trade_episode_id")
    if isinstance(top_level, str) and top_level.strip():
        candidates.add(top_level.strip())

    messages = bundle.get("messages") or []
    if not isinstance(messages, list):
        return None, "MESSAGES_INVALID"

    for message in messages:
        if not isinstance(message, dict):
            continue
        if not _roles(message).intersection(SIGNAL_CONTEXT_ROLES):
            continue
        episode_id = message.get("trade_episode_id")
        if isinstance(episode_id, str) and episode_id.strip():
            candidates.add(episode_id.strip())

    if len(candidates) == 1:
        return next(iter(candidates)), None
    if not candidates:
        return None, "TRADE_EPISODE_NOT_FOUND"
    return None, "TRADE_EPISODE_AMBIGUOUS"


def _load_runtime_and_manual_tp(session_factory, analysis_log_id):
    """Read runtime controls and the latest active manual TP setting."""
    if session_factory is None:
        from app.database.connection import SessionLocal
        session_factory = SessionLocal

    payload = {
        "execution_mode": "APPROVAL",
        "trading_mode": "DRY_RUN",
        "auto_execution_enabled": False,
        "auto_paused": True,
        "manual_tp_pips": None,
    }

    with session_factory() as db:
        state = db.get(RuntimeControlState, 1)
        if state is not None:
            payload["execution_mode"] = state.execution_mode
            payload["trading_mode"] = state.trading_mode
            payload["auto_execution_enabled"] = state.auto_execution_enabled
            payload["auto_paused"] = state.auto_paused

        if analysis_log_id is not None:
            setting = db.scalar(
                select(ManualTPSetting)
                .where(
                    ManualTPSetting.analysis_log_id == analysis_log_id
                )
                .order_by(
                    ManualTPSetting.created_at.desc(),
                    ManualTPSetting.id.desc(),
                )
                .limit(1)
            )
            if (
                setting is not None
                and setting.status == "WAITING_FOR_EXECUTION"
            ):
                payload["manual_tp_pips"] = str(setting.tp_pips)

    return payload


def _read_mt5_snapshot():
    """Read a quote from a configured DEMO terminal, never send orders."""
    from app.config import settings

    terminal_path = getattr(settings, "MT5_TERMINAL_PATH", None)
    expected_server = getattr(settings, "MT5_EXPECTED_SERVER", None)
    symbol = getattr(settings, "MT5_SYMBOL", None)

    if not terminal_path or not symbol:
        return {
            "ok": False,
            "reason": "MT5_CONFIGURATION_MISSING",
            "symbol": symbol,
        }

    connector = None
    connected_attempted = False
    try:
        # Lazy import: unit tests and machines without MT5 can import this module.
        from app.execution import mt5_connector as connector_module
        connector = connector_module
        connected_attempted = True

        connection_info = connector.connect_readonly(
            terminal_path=terminal_path,
            expected_server=expected_server or None,
            require_demo=True,
        )
        spec = connector.get_market_clock_snapshot(symbol)
        account_info = (connection_info or {}).get("account") or {}

        return {
            "ok": True,
            "symbol": spec.get("symbol"),
            "bid": spec.get("bid"),
            "ask": spec.get("ask"),
            "tick_size": spec.get("tick_size"),
            "tick_time": spec.get("tick_time"),
            "tick_time_msc": spec.get("tick_time_msc"),
            "reference_time_msc": spec.get("reference_time_msc"),
            "reference_symbol": spec.get("reference_symbol"),
            "reference_clock_confirmed": spec.get("reference_clock_confirmed"),
            "reference_clock_reason": spec.get("reference_clock_reason"),
            "reference_silence_seconds": spec.get("reference_silence_seconds"),
            "clock_source": spec.get("clock_source"),
            "broker_server": account_info.get("server") or expected_server or None,
            "account_mode": "DEMO",
            "readonly": True,
        }
    except Exception as exc:
        return {
            "ok": False,
            "reason": f"MT5_READ_FAILED:{type(exc).__name__}",
            "symbol": symbol,
        }
    finally:
        if connected_attempted and connector is not None:
            try:
                connector.shutdown()
            except Exception:
                pass


def _select_fresh_price(direction, market_data, now=None):
    """Validate spread, tick age and choose Ask for BUY / Bid for SELL."""
    from app.config import settings

    metadata = {
        "available": False,
        "reason": None,
        "symbol": None,
        "bid": None,
        "ask": None,
        "spread_price": None,
        "spread_pips": None,
        "tick_size": None,
        "tick_time": None,
        "tick_age_seconds": None,
        "price_side": None,
        "current_price": None,
        "readonly": True,
        "account_mode": None,
    }

    if not isinstance(market_data, dict) or market_data.get("ok") is not True:
        metadata["reason"] = (
            market_data.get("reason", "MT5_MARKET_DATA_UNAVAILABLE")
            if isinstance(market_data, dict)
            else "MT5_MARKET_DATA_UNAVAILABLE"
        )
        if isinstance(market_data, dict):
            metadata["symbol"] = market_data.get("symbol")
        return None, None, metadata

    try:
        bid = Decimal(str(market_data.get("bid")))
        ask = Decimal(str(market_data.get("ask")))
        tick_size = Decimal(str(market_data.get("tick_size")))
        relative_clock = (
            market_data.get("tick_time_msc") is not None
            or market_data.get("reference_time_msc") is not None
        )
        if relative_clock:
            if (
                market_data.get("tick_time_msc") is None
                or market_data.get("reference_time_msc") is None
            ):
                raise ValueError("HFM_MARKET_CLOCK_METADATA_INCOMPLETE")
            tick_timestamp = (
                Decimal(str(market_data.get("tick_time_msc")))
                / Decimal("1000")
            )
            reference_timestamp = (
                Decimal(str(market_data.get("reference_time_msc")))
                / Decimal("1000")
            )
        else:
            # Backward-compatible path for legacy injected test snapshots.
            tick_timestamp = Decimal(str(market_data.get("tick_time")))
            reference_timestamp = None

        numeric_values = (bid, ask, tick_size, tick_timestamp)
        if reference_timestamp is not None:
            numeric_values += (reference_timestamp,)
        if not all(item.is_finite() for item in numeric_values):
            raise ValueError("NON_FINITE_MARKET_DATA")
        if (
            bid <= 0
            or ask <= 0
            or tick_size <= 0
            or tick_timestamp <= 0
            or (
                reference_timestamp is not None
                and reference_timestamp <= 0
            )
        ):
            raise ValueError("NON_POSITIVE_MARKET_DATA")
        if ask < bid:
            raise ValueError("INVALID_BID_ASK_SPREAD")

        spread_price = ask - bid
        pip_price = Decimal(str(PIP_PRICE))

        if not pip_price.is_finite() or pip_price <= 0:
            raise ValueError("PIP_PRICE_INVALID")

        spread_pips = spread_price / pip_price

        if direction not in {"BUY", "SELL"}:
            raise ValueError("DIRECTION_MISSING_FOR_PRICE_SELECTION")

        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        max_age = int(getattr(settings, "MT5_MAX_TICK_AGE_SECONDS", 15))
        if max_age <= 0:
            raise ValueError("MT5_MAX_TICK_AGE_SECONDS_INVALID")

        if relative_clock:
            # Reference clock must come from another Market Watch symbol.
            target_symbol = str(
                market_data.get("symbol") or ""
            ).strip()
            reference_symbol = str(
                market_data.get("reference_symbol") or ""
            ).strip()

            if not target_symbol or not reference_symbol:
                raise ValueError("MT5_REFERENCE_SYMBOL_MISSING")

            if (
                reference_symbol.casefold()
                == target_symbol.casefold()
            ):
                raise ValueError("MT5_REFERENCE_CLOCK_SELF_REFERENCE")

            # Both timestamps originate from the same HFM Market Watch clock.
            # The device UTC offset therefore cannot make XAUUSD look stale.
            tick_age = reference_timestamp - tick_timestamp
            if tick_age < 0:
                raise ValueError("MT5_TICK_AFTER_REFERENCE_CLOCK")

            # XAUUSD tidak boleh tertinggal lebih dari batas freshness
            # terhadap quote terbaru di HFM Market Watch.
            if tick_age > Decimal(max_age):
                raise ValueError("MT5_TICK_STALE")

            if market_data.get("reference_clock_confirmed") is not True:
                raise ValueError("MT5_REFERENCE_CLOCK_NOT_CONFIRMED")

            reference_silence = Decimal(
                str(market_data.get("reference_silence_seconds"))
            )
            if not reference_silence.is_finite() or reference_silence < 0:
                raise ValueError("MT5_REFERENCE_SILENCE_INVALID")
            if reference_silence > Decimal(max_age):
                raise ValueError("MT5_REFERENCE_FEED_STALE")
        else:
            # Legacy compatibility for direct unit-test snapshots. Production
            # MT5 snapshots must include the relative Market Watch fields.
            tick_age = Decimal(str(now.timestamp())) - tick_timestamp
            if tick_age < Decimal("-2"):
                raise ValueError("MT5_TICK_FROM_FUTURE")
            if tick_age > Decimal(max_age):
                raise ValueError("MT5_TICK_STALE")

        if direction == "BUY":
            current_price = ask
            price_side = "ASK"
        else:
            current_price = bid
            price_side = "BID"

        metadata.update({
            "available": True,
            "reason": None,
            "symbol": market_data.get("symbol"),
            "bid": str(bid),
            "ask": str(ask),
            "spread_price": str(spread_price),
            "spread_pips": str(spread_pips),
            "tick_size": str(tick_size),
            "tick_time": int(tick_timestamp),
            "tick_age_seconds": float(tick_age),
            "tick_age_basis": (
                "HFM_MARKET_WATCH"
                if relative_clock
                else "DEVICE_UTC_LEGACY"
            ),
            "reference_symbol": market_data.get("reference_symbol"),
            "reference_time_msc": market_data.get("reference_time_msc"),
            "reference_silence_seconds": market_data.get(
                "reference_silence_seconds"
            ),
            "device_clock_offset_seconds": (
                float(reference_timestamp - Decimal(str(now.timestamp())))
                if relative_clock
                else None
            ),
            "price_side": price_side,
            "current_price": str(current_price),
            "readonly": market_data.get("readonly", True),
            "account_mode": market_data.get("account_mode"),
        })
        return str(current_price), str(tick_size), metadata

    except (InvalidOperation, TypeError, ValueError, ArithmeticError) as exc:
        metadata["reason"] = str(exc)[:120] or "MARKET_DATA_INVALID"
        metadata["symbol"] = market_data.get("symbol")
        metadata["bid"] = market_data.get("bid")
        metadata["ask"] = market_data.get("ask")
        metadata["tick_time"] = market_data.get("tick_time")
        return None, None, metadata


def _bundle_roles(bundle):
    all_roles = set()
    for message in (bundle.get("messages") or []) if isinstance(bundle, dict) else []:
        if isinstance(message, dict):
            all_roles.update(_roles(message))
    return all_roles


def evaluate_pipeline_decision(
    ai_result,
    *,
    bundle,
    analysis_log_id,
    session_factory=None,
    market_data=None,
    now=None,
):
    """Evaluate one AI result and return a JSON-serializable decision object.

    `market_data` is an optional test seam. In production it is read through
    the existing read-only MT5 connector. No order function is imported/called.
    """
    if not isinstance(ai_result, dict) or ai_result.get("status") != "ANALYZED":
        return {
            "status": "NOT_EVALUATED",
            "reason": "AI_RESULT_NOT_ANALYZED",
        }

    analysis = ai_result.get("final_analysis")
    if not isinstance(analysis, dict) or not isinstance(analysis.get("extracted"), dict):
        return {
            "status": "NOT_EVALUATED",
            "reason": "FINAL_ANALYSIS_OR_EXTRACTION_MISSING",
        }

    extracted = analysis["extracted"]
    roles = _bundle_roles(bundle)
    actionable_roles = SIGNAL_CONTEXT_ROLES | {"SKIP", "REENTRY_PLAN"}
    if not roles.intersection(actionable_roles):
        return {
            "status": "NOT_APPLICABLE",
            "reason": "NO_ENTRY_OR_SKIP_CONTEXT",
        }

    controls = _load_runtime_and_manual_tp(
        session_factory,
        analysis_log_id,
    )

    direction = extracted.get("direction")
    zone_text = extracted.get("entry_zone_text")
    has_zone = isinstance(zone_text, str) and bool(zone_text.strip())

    episode_id, episode_error = _resolve_episode_id(bundle)

    # A fully specified entry must belong to exactly one persistent episode.
    if has_zone and direction in {"BUY", "SELL"} and episode_id is None:
        return {
            "status": "NEEDS_REVIEW",
            "reason": episode_error or "TRADE_EPISODE_UNRESOLVED",
            "trade_episode_id": None,
            "market_data": {
                "available": False,
                "reason": "PRICE_NOT_READ_BECAUSE_EPISODE_UNRESOLVED",
            },
        }

    if market_data is None:
        market_data = _read_mt5_snapshot()

    current_price, tick_size, market_metadata = _select_fresh_price(
        direction,
        market_data,
        now=now,
    )

    decision = evaluate_signal(
        ai_result,
        execution_mode=controls["execution_mode"],
        trading_mode=controls["trading_mode"],
        auto_execution_enabled=controls["auto_execution_enabled"],
        auto_paused=controls["auto_paused"],
        current_price=current_price,
        tick_size=tick_size,
        spread_pips=market_metadata.get("spread_pips"),
        manual_tp_pips=controls["manual_tp_pips"],
        bundle=bundle,
        trade_episode_id=episode_id,
    )

    if not isinstance(decision, dict):
        return {
            "status": "NEEDS_REVIEW",
            "reason": "DECISION_ENGINE_RETURNED_INVALID_RESULT",
            "market_data": market_metadata,
        }

    output = dict(decision)
    output["market_data"] = market_metadata
    output["runtime"] = {
        "execution_mode": controls["execution_mode"],
        "trading_mode": controls["trading_mode"],
        "auto_execution_enabled": controls["auto_execution_enabled"],
        "auto_paused": controls["auto_paused"],
    }
    output["manual_tp_applied"] = controls["manual_tp_pips"] is not None
    output["order_sent"] = False
    return output


def map_decision_to_analysis_status(decision):
    """Map decision status to existing AIAnalysisLog / approval workflow."""
    if not isinstance(decision, dict):
        return "MANUAL_REVIEW"

    status = decision.get("status")

    if status == "NO_SIGNAL":
        return "NO_SIGNAL"

    if status in DECISION_REVIEW_STATUSES:
        return "MANUAL_REVIEW"

    # A later Telegram message can complete these waiting states.
    if status in WAITING_DECISION_STATUSES:
        return "ANALYZED"

    if status in {"READY_FOR_DRY_RUN", "NOT_APPLICABLE", "NOT_EVALUATED"}:
        return "ANALYZED"

    return "MANUAL_REVIEW"

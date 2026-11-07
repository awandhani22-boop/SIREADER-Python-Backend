from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import (
    Base,
    AIAnalysisLog,
    ManualTPSetting,
    RuntimeControlState,
)
from app.signals.decision_service import (
    evaluate_pipeline_decision,
    map_decision_to_analysis_status,
    _select_fresh_price,
)

CHAT_ID = -1003014486607


@pytest.fixture
def db_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


def add_analysis(factory):
    with factory() as db:
        row = AIAnalysisLog(
            chat_id=CHAT_ID,
            target_message_id=1001,
            status="PROCESSING",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


def sample_ai(direction="BUY", zone="4100-4102"):
    return {
        "status": "ANALYZED",
        "final_analysis": {
            "status": "CLEAR",
            "confidence": 0.95,
            "extracted": {
                "direction": direction,
                "entry_zone_text": zone,
                "explicit_entry_text": None,
                "stop_loss_text": None,
                "take_profit_texts": ["TP 100 pips"],
                "conditions": [],
                "action_instructions": [],
            },
            "evidence": [],
            "uncertainties": [],
        },
    }


def sample_bundle(direction="BUY"):
    return {
        "chat_id": CHAT_ID,
        "target_message_id": 1001,
        "messages": [
            {
                "telegram_message_id": 1001,
                "message_roles": ["NEW_SIGNAL"],
                "message_role": "NEW_SIGNAL",
                "trade_episode_id": f"trade_{CHAT_ID}_1001",
                "trade_episode_status": "OPEN",
                "text": f"{direction} XAUUSD 4100-4102",
            }
        ],
    }


def fresh_market_data(now):
    return {
        "ok": True,
        "symbol": "XAUUSDb-DEMO",
        "bid": "4101.00",
        "ask": "4101.20",
        "tick_size": "0.01",
        "tick_time": int(now.timestamp()),
        "readonly": True,
        "account_mode": "DEMO",
    }


def test_buy_memakai_ask_dan_manual_tp(db_factory):
    log_id = add_analysis(db_factory)
    with db_factory() as db:
        db.add(ManualTPSetting(
            analysis_log_id=log_id,
            symbol="XAUUSDb",
            tp_pips="50",
            price_distance="5.0",
            set_by_user_id=12345,
            status="WAITING_FOR_EXECUTION",
        ))
        db.commit()

    now = datetime.now(timezone.utc)
    result = evaluate_pipeline_decision(
        sample_ai("BUY"),
        bundle=sample_bundle("BUY"),
        analysis_log_id=log_id,
        session_factory=db_factory,
        market_data=fresh_market_data(now),
        now=now,
    )

    assert result["status"] == "READY_FOR_APPROVAL"
    assert result["market_data"]["price_side"] == "ASK"
    assert result["current_price"] == "4101.2"
    assert result["take_profit_source"] == "MANUAL"
    assert result["take_profit_targets_pips"] == ["50"]
    assert result["order_sent"] is False
    assert result["manual_tp_applied"] is True


def test_sell_memakai_bid(db_factory):
    log_id = add_analysis(db_factory)
    now = datetime.now(timezone.utc)
    result = evaluate_pipeline_decision(
        sample_ai("SELL"),
        bundle=sample_bundle("SELL"),
        analysis_log_id=log_id,
        session_factory=db_factory,
        market_data=fresh_market_data(now),
        now=now,
    )

    assert result["status"] == "READY_FOR_APPROVAL", (
    f"STATUS: {result.get('status')}\n"
    f"REASON: {result.get('reason')}\n"
    f"RESULT: {result}"
)
    assert result["market_data"]["price_side"] == "BID"
    assert result["current_price"] == "4101"
    assert result["order_sent"] is False


def test_tick_stale_ditolak(db_factory):
    log_id = add_analysis(db_factory)
    now = datetime.now(timezone.utc)
    data = fresh_market_data(now)
    data["tick_time"] = int(now.timestamp()) - 120

    result = evaluate_pipeline_decision(
        sample_ai("BUY"),
        bundle=sample_bundle("BUY"),
        analysis_log_id=log_id,
        session_factory=db_factory,
        market_data=data,
        now=now,
    )

    assert result["status"] == "NEEDS_REVIEW"
    assert result["market_data"]["available"] is False
    assert result["market_data"]["reason"] == "MT5_TICK_STALE"


def test_episode_ambigu_ditolak(db_factory):
    log_id = add_analysis(db_factory)
    bundle = sample_bundle("BUY")
    bundle["messages"].append({
        "telegram_message_id": 1002,
        "message_roles": ["NEW_SIGNAL"],
        "trade_episode_id": "trade_other_episode",
        "text": "BUY XAUUSD 4100-4102",
    })

    result = evaluate_pipeline_decision(
        sample_ai("BUY"),
        bundle=bundle,
        analysis_log_id=log_id,
        session_factory=db_factory,
        market_data={"ok": False, "reason": "SHOULD_NOT_BE_USED"},
    )

    assert result["status"] == "NEEDS_REVIEW"
    assert result["reason"] == "TRADE_EPISODE_AMBIGUOUS"


def test_try_sell_tanpa_zona_menunggu_zona(db_factory):
    log_id = add_analysis(db_factory)
    bundle = {
        "chat_id": CHAT_ID,
        "target_message_id": 1001,
        "messages": [{
            "telegram_message_id": 1001,
            "message_roles": ["ENTRY_REQUEST"],
            "message_role": "ENTRY_REQUEST",
            "pending_direction": "SELL",
            "text": "Try sell",
        }],
    }
    ai = sample_ai("SELL", None)

    result = evaluate_pipeline_decision(
        ai,
        bundle=bundle,
        analysis_log_id=log_id,
        session_factory=db_factory,
        market_data={"ok": False, "reason": "NO_QUOTE_NEEDED_YET"},
    )

    assert result["status"] == "WAITING_FOR_ZONE"
    assert map_decision_to_analysis_status(result) == "ANALYZED"


def test_mapping_status_ke_approval():
    assert map_decision_to_analysis_status({
        "status": "READY_FOR_APPROVAL"
    }) == "MANUAL_REVIEW"
    assert map_decision_to_analysis_status({
        "status": "WAITING_FOR_CISD"
    }) == "ANALYZED"
    assert map_decision_to_analysis_status({
        "status": "WAITING_FOR_SPREAD"
    }) == "ANALYZED"
    assert map_decision_to_analysis_status({
        "status": "NO_SIGNAL"
    }) == "NO_SIGNAL"

def test_spread_di_atas_sl_menunggu_dan_tidak_mengirim_order(db_factory):
    log_id = add_analysis(db_factory)
    now = datetime.now(timezone.utc)

    data = fresh_market_data(now)

    # BUY memakai ASK = 4101.10, hanya 1 pip dari midpoint 4101.
    # Spread = 3.10 / 0.10 = 31 pips.
    data["bid"] = "4098.00"
    data["ask"] = "4101.10"

    result = evaluate_pipeline_decision(
        sample_ai("BUY"),
        bundle=sample_bundle("BUY"),
        analysis_log_id=log_id,
        session_factory=db_factory,
        market_data=data,
        now=now,
    )

    assert result["status"] == "WAITING_FOR_SPREAD"
    assert result["reason"] == "SPREAD_EXCEEDS_STOP_LOSS"
    assert result["market_data"]["spread_price"] == "3.10"
    assert result["market_data"]["spread_pips"] == "31"
    assert result["spread_pips"] == "31"
    assert result["maximum_spread_pips"] == "30"
    assert result["order_sent"] is False
    assert map_decision_to_analysis_status(result) == "ANALYZED"

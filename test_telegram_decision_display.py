from app.approval.telegram_bot import review_detail_text


def test_review_detail_menampilkan_decision_engine():
    detail = {
        "analysis_log_id": 22,
        "target_message_id": 123,
        "status": "MANUAL_REVIEW",
        "result": {
            "decision": {
                "status": "READY_FOR_APPROVAL",
                "reason": "TEST_READY",
                "trade_episode_id": "trade_-1003014486607_123",
                "market_data": {
                    "available": True,
                    "reason": None,
                    "symbol": "XAUUSDb-DEMO",
                    "bid": "4101",
                    "ask": "4101.2",
                    "price_side": "ASK",
                    "current_price": "4101.2",
                    "tick_age_seconds": 0.7,
                    "account_mode": "DEMO",
                    "readonly": True,
                },
                "runtime": {
                    "execution_mode": "APPROVAL",
                    "trading_mode": "DRY_RUN",
                    "auto_execution_enabled": False,
                    "auto_paused": True,
                },
                "order_sent": False,
            }
        },
        "final_analysis": {
            "confidence": 0.95,
            "extracted": {
                "direction": "BUY",
                "entry_zone_text": "4100-4102",
            },
            "evidence": [],
            "uncertainties": [],
        },
        "source_bundle": {
            "messages": [],
        },
    }

    output = review_detail_text(detail, None)

    assert "DECISION ENGINE" in output
    assert "Decision status: READY_FOR_APPROVAL" in output
    assert "Decision reason: TEST_READY" in output
    assert "MT5 symbol: XAUUSDb-DEMO" in output
    assert "Price side: ASK" in output
    assert "Reference price: 4101.2" in output
    assert "Account mode: DEMO" in output
    assert "Execution mode: APPROVAL" in output
    assert "Trading mode: DRY_RUN" in output
    assert "Order sent: False" in output

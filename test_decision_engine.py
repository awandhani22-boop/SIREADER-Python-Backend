
from app.signals.decision_engine import evaluate_signal


# ============================================================
# TEST FIXTURES
# ============================================================

def make_ai_result(
    *,
    router_status="ANALYZED",
    analysis_status="CLEAR",
    direction="SELL",
    zone="4110-4112",
    conditions=None,
    actions=None,
    tp_texts=None,
):
    return {
        "status": router_status,
        "final_analysis": {
            "status": analysis_status,
            "confidence": 0.95,
            "needs_reasoning": False,
            "needs_escalation": False,
            "critical_ambiguity": False,
            "summary": "Test analysis",
            "extracted": {
                "direction": direction,
                "entry_zone_text": zone,
                "explicit_entry_text": None,
                "stop_loss_text": None,
                "take_profit_texts": (
                    tp_texts if tp_texts is not None else []
                ),
                "conditions": (
                    conditions if conditions is not None else []
                ),
                "action_instructions": (
                    actions if actions is not None else []
                ),
            },
            "evidence": [],
            "uncertainties": [],
        },
        "attempts": [],
    }


def evaluate(
    ai_result=None,
    **overrides,
):
    """
    Helper evaluasi dengan konfigurasi default.

    None pada ai_result berarti gunakan fixture normal.
    Untuk menguji input AI None secara langsung,
    panggil evaluate_signal(None) tanpa helper ini.
    """

    arguments = {
        "execution_mode": "APPROVAL",
        "trading_mode": "DRY_RUN",
        "auto_execution_enabled": False,
        "auto_paused": True,
        "current_price": "4111.5",
        "tick_size": "0.01",
    }

    arguments.update(overrides)

    if ai_result is None:
        ai_result = make_ai_result()

    return evaluate_signal(
        ai_result,
        **arguments,
    )


# ============================================================
# BASIC ENTRY DECISIONS
# ============================================================

def test_sell_zone_menghasilkan_keputusan_approval():

    result = evaluate(
        make_ai_result(
            direction="SELL",
            zone="4110-4112",
            tp_texts=["TP 100 pips"],
        )
    )

    assert result["status"] == "READY_FOR_APPROVAL"
    assert result["direction"] == "SELL"

    assert result["entry_zone"] == {
        "low": "4110",
        "high": "4112",
        "midpoint": "4111",
    }

    assert result["entry_price_reference"] == "4111.5"
    assert result["entry_type"] == "MARKET"

    assert result["stop_loss_pips"] == "30"
    assert result["stop_loss_price_reference"] == "4114.5"

    assert result["take_profit_targets_pips"] == ["100"]
    assert result["take_profit_prices_reference"] == ["4101.5"]
    assert result["take_profit_source"] == "MENTOR"


def test_buy_zone_menghasilkan_sl_di_bawah_entry():

    result = evaluate(
        make_ai_result(
            direction="BUY",
            zone="4110-4112",
            tp_texts=["60 pips"],
        )
    )

    assert result["status"] == "READY_FOR_APPROVAL"
    assert result["direction"] == "BUY"

    assert result["entry_price_reference"] == "4111.5"
    assert result["entry_type"] == "MARKET"

    assert result["stop_loss_pips"] == "30"
    assert result["stop_loss_price_reference"] == "4108.5"

    assert result["take_profit_targets_pips"] == ["60"]
    assert result["take_profit_prices_reference"] == ["4117.5"]


def test_zona_bisa_menggunakan_kata_sampai():

    result = evaluate(
        make_ai_result(
            direction="BUY",
            zone="4110 sampai 4112",
        )
    )

    assert result["status"] == "READY_FOR_APPROVAL"
    assert result["entry_price_reference"] == "4111.5"


# ============================================================
# ENTRY ZONE VALIDATION
# ============================================================

def test_tanpa_zona_menunggu_zona():

    result = evaluate(
        make_ai_result(zone=None),
    )

    assert result["status"] == "WAITING_FOR_ZONE"
    assert result["reason"] == "ENTRY_ZONE_NOT_PROVIDED"


def test_zona_ambigu_masuk_review():

    result = evaluate(
        make_ai_result(
            zone="4110-4112 atau 4120-4122",
        )
    )

    assert result["status"] == "NEEDS_REVIEW"
    assert result["reason"].startswith("ENTRY_ZONE_INVALID")


def test_zona_terbalik_masuk_review():

    result = evaluate(
        make_ai_result(
            zone="4112-4110",
        )
    )

    assert result["status"] == "NEEDS_REVIEW"
    assert result["reason"].startswith("ENTRY_ZONE_INVALID")


def test_direction_tidak_valid_masuk_review():

    result = evaluate(
        make_ai_result(direction=None),
    )

    assert result["status"] == "NEEDS_REVIEW"
    assert result["reason"] == "DIRECTION_MISSING_OR_INVALID"


# ============================================================
# CISD VALIDATION
# ============================================================

def test_approval_menunggu_cisd():

    result = evaluate(
        make_ai_result(
            conditions=[
                "Lowrisk entry, tunggu CISD M1 confirmation",
            ],
        )
    )

    assert result["status"] == "WAITING_FOR_CISD"
    assert result["requires_cisd"] is True
    assert result["cisd_confirmed"] is False


def test_approval_lanjut_setelah_entry_confirmation():

    bundle = {
        "messages": [
            {
                "telegram_message_id": 122,
                "message_role": "PRICE_ZONE",
                "message_roles": ["PRICE_ZONE"],
                "text": "4110-4112",
                "trade_episode_id": "episode_1",
            },
            {
                "telegram_message_id": 123,
                "message_role": "ENTRY_CONFIRMATION",
                "message_roles": ["ENTRY_CONFIRMATION"],
                "text": "CISD M1 valid",
                "trade_episode_id": "episode_1",
            },
        ]
    }

    result = evaluate(
        make_ai_result(
            conditions=[
                "Lowrisk entry, tunggu CISD M1",
            ],
        ),
        bundle=bundle,
    )

    assert result["status"] == "READY_FOR_APPROVAL"
    assert result["requires_cisd"] is True
    assert result["cisd_confirmed"] is True
    assert result["trade_episode_id"] == "episode_1"


def test_confirmation_episode_lain_ditolak():

    bundle = {
        "messages": [
            {
                "telegram_message_id": 122,
                "message_role": "PRICE_ZONE",
                "message_roles": ["PRICE_ZONE"],
                "text": "4110-4112",
                "trade_episode_id": "episode_1",
            },
            {
                "telegram_message_id": 123,
                "message_role": "ENTRY_CONFIRMATION",
                "message_roles": ["ENTRY_CONFIRMATION"],
                "text": "CISD M1 valid",
                "trade_episode_id": "episode_2",
            },
        ]
    }

    result = evaluate(
        make_ai_result(
            conditions=[
                "Lowrisk entry, tunggu CISD M1",
            ],
        ),
        bundle=bundle,
    )

    assert result["status"] == "WAITING_FOR_CISD"
    assert result["cisd_confirmed"] is False
    assert result["trade_episode_id"] == "episode_1"


def test_confirmation_tanpa_episode_id_ditolak():

    bundle = {
        "messages": [
            {
                "telegram_message_id": 122,
                "message_role": "PRICE_ZONE",
                "message_roles": ["PRICE_ZONE"],
                "text": "4110-4112",
                "trade_episode_id": "episode_1",
            },
            {
                "telegram_message_id": 123,
                "message_role": "ENTRY_CONFIRMATION",
                "message_roles": ["ENTRY_CONFIRMATION"],
                "text": "CISD M1 valid",
                "trade_episode_id": None,
            },
        ]
    }

    result = evaluate(
        make_ai_result(
            conditions=[
                "Lowrisk entry, tunggu CISD M1",
            ],
        ),
        bundle=bundle,
    )

    assert result["status"] == "WAITING_FOR_CISD"
    assert result["cisd_confirmed"] is False
    assert result["trade_episode_id"] == "episode_1"


def test_auto_mengabaikan_syarat_cisd():

    result = evaluate(
        make_ai_result(
            conditions=[
                "Lowrisk entry, tunggu CISD M1",
            ],
        ),
        execution_mode="AUTO",
        trading_mode="DRY_RUN",
        auto_execution_enabled=True,
        auto_paused=False,
    )

    assert result["status"] == "READY_FOR_DRY_RUN"
    assert result["requires_cisd"] is True
    assert result["cisd_confirmed"] is False


def test_auto_yang_disabled_tidak_bisa_diproses():

    result = evaluate(
        execution_mode="AUTO",
        trading_mode="DRY_RUN",
        auto_execution_enabled=False,
        auto_paused=False,
    )

    assert result["status"] == "AUTO_PAUSED"


def test_auto_yang_paused_tidak_bisa_diproses():

    result = evaluate(
        execution_mode="AUTO",
        trading_mode="DRY_RUN",
        auto_execution_enabled=True,
        auto_paused=True,
    )

    assert result["status"] == "AUTO_PAUSED"


def test_auto_live_ditolak_oleh_decision_engine():

    result = evaluate(
        execution_mode="AUTO",
        trading_mode="LIVE",
        auto_execution_enabled=True,
        auto_paused=False,
    )

    assert result["status"] == "NEEDS_REVIEW"

    assert result["reason"] == (
        "AUTO_LIVE_NOT_SUPPORTED_BY_DECISION_ENGINE"
    )


# ============================================================
# PRICE DISTANCE VALIDATION
# ============================================================

def test_harga_lebih_dari_30_pips_dari_midpoint_diskip():

    result = evaluate(
        current_price="4115",
    )

    assert result["status"] == "SKIP"

    assert result["reason"] == (
        "ENTRY_PRICE_TOO_FAR_FROM_MIDPOINT"
    )

    assert result["skip_scope"] == "NEW_ENTRY_ONLY"


def test_harga_tepat_30_pips_masih_dapat_diproses():

    result = evaluate(
        current_price="4114",
    )

    assert result["status"] == "READY_FOR_APPROVAL"
    assert result["entry_type"] == "SELL_STOP"


def test_harga_di_dalam_zona_menggunakan_market():

    result = evaluate(
        current_price="4111.5",
    )

    assert result["status"] == "READY_FOR_APPROVAL"
    assert result["entry_type"] == "MARKET"


# ============================================================
# PENDING ORDER CLASSIFICATION
# ============================================================

def test_sell_di_atas_zona_menggunakan_sell_stop():

    result = evaluate(
        current_price="4113",
    )

    assert result["status"] == "READY_FOR_APPROVAL"
    assert result["entry_type"] == "SELL_STOP"

    assert result["entry_price_reference"] == "4111"


def test_sell_di_bawah_zona_menggunakan_sell_limit():

    result = evaluate(
        current_price="4109",
    )

    assert result["status"] == "READY_FOR_APPROVAL"
    assert result["entry_type"] == "SELL_LIMIT"

    assert result["entry_price_reference"] == "4111"


def test_buy_di_bawah_zona_menggunakan_buy_stop():

    result = evaluate(
        make_ai_result(direction="BUY"),
        current_price="4109",
    )

    assert result["status"] == "READY_FOR_APPROVAL"
    assert result["entry_type"] == "BUY_STOP"

    assert result["entry_price_reference"] == "4111"


def test_buy_di_atas_zona_menggunakan_buy_limit():

    result = evaluate(
        make_ai_result(direction="BUY"),
        current_price="4113",
    )

    assert result["status"] == "READY_FOR_APPROVAL"
    assert result["entry_type"] == "BUY_LIMIT"

    assert result["entry_price_reference"] == "4111"


# ============================================================
# PRICE DATA VALIDATION
# ============================================================

def test_current_price_wajib_ada():

    result = evaluate(
        current_price=None,
    )

    assert result["status"] == "NEEDS_REVIEW"

    assert result["reason"].startswith(
        "PRICE_DATA_INVALID"
    )


def test_tick_size_wajib_valid():

    result = evaluate(
        tick_size="0",
    )

    assert result["status"] == "NEEDS_REVIEW"

    assert result["reason"].startswith(
        "PRICE_DATA_INVALID"
    )


# ============================================================
# TAKE PROFIT
# ============================================================

def test_tp_mentor_dapat_diekstrak():

    result = evaluate(
        make_ai_result(
            tp_texts=[
                "TP1 60 pips",
                "TP2 100 pips",
            ],
        )
    )

    assert result["take_profit_targets_pips"] == [
        "60",
        "100",
    ]

    assert result["take_profit_source"] == "MENTOR"


def test_manual_tp_mengalahkan_tp_mentor():

    result = evaluate(
        make_ai_result(
            direction="BUY",
            tp_texts=["TP 60 pips"],
        ),
        manual_tp_pips=100,
    )

    assert result["take_profit_targets_pips"] == ["100"]

    assert result["take_profit_source"] == "MANUAL"

    assert result["take_profit_prices_reference"] == [
        "4121.5"
    ]


def test_tp_tidak_dikarang_jika_tidak_disebutkan():

    result = evaluate(
        make_ai_result(
            tp_texts=[],
        ),
    )

    assert result["status"] == "READY_FOR_APPROVAL"

    assert result["take_profit_targets_pips"] == []
    assert result["take_profit_prices_reference"] == []

    assert result["take_profit_source"] == "NONE"


def test_tp_tanpa_satuan_pips_masuk_review():

    result = evaluate(
        make_ai_result(
            tp_texts=["TP 4200"],
        )
    )

    assert result["status"] == "NEEDS_REVIEW"

    assert result["reason"].startswith(
        "TAKE_PROFIT_INVALID"
    )


def test_manual_tp_nol_ditolak():

    result = evaluate(
        manual_tp_pips=0,
    )

    assert result["status"] == "NEEDS_REVIEW"


# ============================================================
# SKIP AND EPISODE SAFETY
# ============================================================

def test_skip_dengan_episode_id_yang_jelas():

    bundle = {
        "messages": [
            {
                "telegram_message_id": 500,
                "message_role": "SKIP",
                "message_roles": ["SKIP"],
                "trade_episode_id": "episode_1",
                "text": "Skip",
            }
        ]
    }

    result = evaluate(
        make_ai_result(
            actions=["Skip"],
        ),
        bundle=bundle,
    )

    assert result["status"] == "SKIP"

    assert result["reason"] == (
        "MENTOR_SKIP_INSTRUCTION"
    )

    assert result["trade_episode_id"] == "episode_1"

    assert result["skip_scope"] == (
        "TARGET_EPISODE_ONLY"
    )

    assert result["requires_mt5_reconciliation"] is True


def test_skip_tanpa_episode_id_masuk_review():

    result = evaluate(
        make_ai_result(
            actions=["Skip"],
        )
    )

    assert result["status"] == "NEEDS_REVIEW"

    assert result["reason"] == (
        "SKIP_TARGET_EPISODE_AMBIGUOUS"
    )


def test_jangan_skip_bukan_instruksi_skip():

    result = evaluate(
        make_ai_result(
            actions=["Jangan skip setup ini"],
        )
    )

    assert result["status"] == "READY_FOR_APPROVAL"


def test_reentry_plan_saja_bukan_entry():

    bundle = {
        "messages": [
            {
                "telegram_message_id": 600,
                "message_role": "REENTRY_PLAN",
                "message_roles": ["REENTRY_PLAN"],
                "text": "Ntar ku carikan re entry",
            }
        ]
    }

    result = evaluate(
        make_ai_result(),
        bundle=bundle,
    )

    assert result["status"] == "NO_SIGNAL"

    assert result["reason"] == (
        "REENTRY_PLAN_ONLY"
    )


# ============================================================
# AI ROUTER STATUS VALIDATION
# ============================================================

def test_router_manual_review_tidak_boleh_menjadi_entry():

    result = evaluate(
        make_ai_result(
            router_status="MANUAL_REVIEW",
        )
    )

    assert result["status"] == "NEEDS_REVIEW"

    assert result["reason"] == (
        "AI_ROUTER_STATUS_MANUAL_REVIEW"
    )


def test_router_no_signal_tidak_membuat_setup():

    result = evaluate(
        make_ai_result(
            router_status="NO_SIGNAL",
        )
    )

    assert result["status"] == "NO_SIGNAL"


def test_analysis_unresolved_masuk_review():

    result = evaluate(
        make_ai_result(
            analysis_status="UNRESOLVED",
        )
    )

    assert result["status"] == "NEEDS_REVIEW"

    assert result["reason"] == (
        "ANALYSIS_STATUS_UNRESOLVED"
    )


def test_ai_result_invalid_masuk_review():

    result = evaluate_signal(
        None,
        current_price="4111",
        tick_size="0.01",
    )

    assert result["status"] == "NEEDS_REVIEW"

    assert result["reason"] == (
        "AI_RESULT_INVALID"
    )

def test_spread_di_bawah_sl_diperbolehkan():
    result = evaluate(spread_pips="29.9")

    assert result["status"] == "READY_FOR_APPROVAL"
    assert result["spread_pips"] == "29.9"
    assert result["maximum_spread_pips"] == "30"


def test_spread_tepat_sama_dengan_sl_diperbolehkan():
    result = evaluate(
        current_price="4114",
        spread_pips="30",
    )

    assert result["status"] == "READY_FOR_APPROVAL"
    assert result["spread_pips"] == "30"
    assert result["maximum_spread_pips"] == "30"


def test_spread_di_atas_sl_menunggu():
    result = evaluate(spread_pips="30.1")

    assert result["status"] == "WAITING_FOR_SPREAD"
    assert result["reason"] == "SPREAD_EXCEEDS_STOP_LOSS"
    assert result["spread_pips"] == "30.1"
    assert result["maximum_spread_pips"] == "30"


def test_harga_terlalu_jauh_diprioritaskan_dari_spread():
    result = evaluate(
        current_price="4114.01",
        spread_pips="40",
    )

    assert result["status"] == "SKIP"
    assert result["reason"] == "ENTRY_PRICE_TOO_FAR_FROM_MIDPOINT"


def test_skip_harga_diprioritaskan_dari_cisd():
    ai = make_ai_result(
        conditions=[
            "Lowrisk entry, tunggu CISD M1 confirmation",
        ],
    )

    result = evaluate(
        ai,
        current_price="4114.01",
        spread_pips="1",
    )

    assert result["status"] == "SKIP"
    assert result["reason"] == "ENTRY_PRICE_TOO_FAR_FROM_MIDPOINT"


def test_spread_invalid_masuk_review():
    result = evaluate(spread_pips="-1")

    assert result["status"] == "NEEDS_REVIEW"
    assert result["reason"].startswith("SPREAD_DATA_INVALID:")

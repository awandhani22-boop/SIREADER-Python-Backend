import pytest
import json

from app.ai import ai_router


BUNDLE = {
    "chat_id": -1003014486607,
    "target_message_id": 123,
    "window_minutes": 10,
    "messages": [
        {
            "telegram_message_id": 123,
            "text": "Pantau sell",
            "sender": None,
            "reply_to_id": None,
            "received_at": "2026-11-03T05:00:00+00:00",
            "telegram_date": "2026-11-03T05:00:00+00:00",
            "is_edited": False,
            "media": [],
            "edit_history": [],
        }
    ],
}


def mock_output(
    status="CLEAR",
    confidence=0.95,
    needs_reasoning=False,
    needs_escalation=False,
    critical_ambiguity=False,
):
    return json.dumps({
        "status": status,
        "confidence": confidence,
        "needs_reasoning": needs_reasoning,
        "needs_escalation": needs_escalation,
        "critical_ambiguity": critical_ambiguity,
        "summary": "Mentor meminta memantau sell.",
        "extracted": {
            "direction": "SELL",
            "entry_zone_text": None,
            "explicit_entry_text": None,
            "stop_loss_text": None,
            "take_profit_texts": [],
            "conditions": [],
            "action_instructions": [],
        },
        "evidence": [
            {
                "message_id": 123,
                "field": "direction",
                "quote": "Pantau sell",
            }
        ],
        "uncertainties": [],
    })


def fake_response(tier, content):
    return {
        "analysis": {
            "model": f"TEST-{tier}",
            "content": content,
            "cost_rp": 0,
            "usage": {},
        }
    }


def test_haiku_selesai_sendiri(monkeypatch):
    monkeypatch.setattr(
        ai_router,
        "_run_tier",
        lambda tier, messages: fake_response(
            tier,
            mock_output(),
        ),
    )

    result = ai_router.analyze_bundle(BUNDLE)

    assert result["status"] == "ANALYZED"
    assert [x["tier"] for x in result["attempts"]] == ["FAST"]

    print("LULUS: Haiku bekerja sendiri.")


def test_tiga_tingkat(monkeypatch):
    outputs = {
        "FAST": mock_output(
            status="NEEDS_REASONING",
            confidence=0.50,
            needs_reasoning=True,
        ),
        "REASONING": mock_output(
            status="UNRESOLVED",
            confidence=0.40,
            needs_reasoning=True,
            needs_escalation=True,
            critical_ambiguity=True,
        ),
        "ESCALATION": mock_output(),
    }

    monkeypatch.setattr(
        ai_router,
        "can_use_expensive_tier",
        lambda tier, messages: True,
    )

    monkeypatch.setattr(
        ai_router,
        "_run_tier",
        lambda tier, messages: fake_response(
            tier,
            outputs[tier],
        ),
    )

    result = ai_router.analyze_bundle(BUNDLE)

    tiers = [x["tier"] for x in result["attempts"]]

    assert tiers == ["FAST", "REASONING", "ESCALATION"]
    assert result["status"] == "ANALYZED"

    print("LULUS: Eskalasi tiga model berjalan berurutan.")


def test_evidence_palsu_ditolak():
    bad = mock_output().replace(
        "Pantau sell",
        "BUY 4000",
    )

    prepared = ai_router.prepare_bundle(BUNDLE)

    try:
        ai_router.parse_model_output(bad, prepared)
    except ai_router.AIOutputError:
        print("LULUS: Evidence palsu ditolak.")
        return

    raise AssertionError("Evidence palsu tidak ditolak!")


if __name__ == "__main__":
    test_haiku_selesai_sendiri()
    test_tiga_tingkat()
    test_evidence_palsu_ditolak()

    print("\nSEMUA TES ROUTER BERHASIL.")

def test_media_dikeluarkan_dari_payload_ai():
    bundle = {
        "target_message_id": 123,
        "messages": [
            {
                "telegram_message_id": 123,
                "text": "Pantau sell",
                "media": [
                    {
                        "media_type": "photo",
                        "file_path": "data/media/chart.jpg",
                    },
                    {
                        "media_type": "video",
                    },
                    {
                        "media_type": "sticker",
                    },
                ],
            }
        ],
    }

    prepared = ai_router.prepare_bundle(bundle)

    assert prepared["messages"] == [
        {
            "message_id": 123,
            "text": "Pantau sell",
        }
    ]

    messages = ai_router.build_messages(
        bundle,
        stage="TEXT_ONLY_TEST",
    )

    payload = json.loads(messages[1]["content"])

    assert payload["bundle"] == prepared
    assert "media_types" not in payload["bundle"]["messages"][0]

def test_parser_menerima_json_dengan_code_fence():
    prepared = ai_router.prepare_bundle(BUNDLE)

    content = (
        "Berikut hasil analisis:\n"
        "```json\n"
        + mock_output()
        + "\n```\n"
        "Selesai."
    )

    result = ai_router.parse_model_output(
        content,
        prepared,
    )

    assert result["status"] == "CLEAR"
    assert result["extracted"]["direction"] == "SELL"


def test_parser_memberikan_diagnostik_json_invalid():
    prepared = ai_router.prepare_bundle(BUNDLE)

    with pytest.raises(ai_router.AIOutputError) as error:
        ai_router.parse_model_output(
            "Saya belum dapat membuat JSON.",
            prepared,
        )

    assert "Awal respons" in str(error.value)

def test_resume_escalation_tidak_mengulang_tier_sebelumnya(monkeypatch):
    calls = []

    outputs = {
        "FAST": mock_output(
            status="NEEDS_REASONING",
            confidence=0.50,
            needs_reasoning=True,
        ),
        "REASONING": mock_output(
            status="UNRESOLVED",
            confidence=0.40,
            needs_reasoning=True,
            needs_escalation=True,
            critical_ambiguity=True,
        ),
        "ESCALATION": mock_output(),
    }

    monkeypatch.setattr(
        ai_router,
        "_run_tier",
        lambda tier, messages: (
            calls.append(tier)
            or fake_response(tier, outputs[tier])
        ),
    )

    # Tahap pertama: FAST dan REASONING berhasil.
    # ESCALATION diblokir.
    monkeypatch.setattr(
        ai_router,
        "can_use_expensive_tier",
        lambda tier, messages: tier != "ESCALATION",
    )

    first = ai_router.analyze_bundle(BUNDLE)

    assert first["status"] == "MANUAL_REVIEW_BUDGET"
    assert first["resume_state"]["next_tier"] == "ESCALATION"
    assert calls == ["FAST", "REASONING"]

    saved_state = first["resume_state"]

    # Tahap kedua: hanya ESCALATION yang dijalankan.
    monkeypatch.setattr(
        ai_router,
        "can_use_expensive_tier",
        lambda tier, messages: True,
    )

    second = ai_router.analyze_bundle(
        BUNDLE,
        resume_state=saved_state,
    )

    assert second["status"] == "ANALYZED"
    assert calls == ["FAST", "REASONING", "ESCALATION"]
    assert [item["tier"] for item in second["attempts"]] == [
        "FAST",
        "REASONING",
        "ESCALATION",
    ]


def test_resume_fast_refusal_tidak_mengulang_fast(monkeypatch):
    calls = []

    def fake_run_tier(tier, messages):
        calls.append(tier)

        if tier == "FAST":
            return fake_response(
                tier,
                "I can't help with this request.",
            )

        return fake_response(
            tier,
            mock_output(),
        )

    monkeypatch.setattr(
        ai_router,
        "_run_tier",
        fake_run_tier,
    )

    # Tahap pertama: FAST refusal, fallback diblokir.
    monkeypatch.setattr(
        ai_router,
        "can_use_expensive_tier",
        lambda tier, messages: False,
    )

    first = ai_router.analyze_bundle(BUNDLE)

    assert first["status"] == "MANUAL_REVIEW_BUDGET"
    assert first["resume_state"]["next_tier"] == "REASONING"
    assert first["resume_state"]["path"] == "refusal_fallback"
    assert calls == ["FAST"]

    # Tahap kedua: fallback diizinkan.
    monkeypatch.setattr(
        ai_router,
        "can_use_expensive_tier",
        lambda tier, messages: True,
    )

    second = ai_router.analyze_bundle(
        BUNDLE,
        resume_state=first["resume_state"],
    )

    assert second["status"] == "ANALYZED"
    assert second["fallback_from"] == "FAST_REFUSAL"
    assert calls == ["FAST", "REASONING"]



def test_resume_reasoning_tidak_mengulang_fast(monkeypatch):
    calls = []

    outputs = {
        "FAST": mock_output(
            status="NEEDS_REASONING",
            confidence=0.50,
            needs_reasoning=True,
        ),
        "REASONING": mock_output(),
    }

    def fake_run_tier(tier, messages):
        calls.append(tier)
        return fake_response(tier, outputs[tier])

    monkeypatch.setattr(
        ai_router,
        "_run_tier",
        fake_run_tier,
    )

    # Tahap pertama: FAST berhasil, REASONING diblokir.
    monkeypatch.setattr(
        ai_router,
        "can_use_expensive_tier",
        lambda tier, messages: tier != "REASONING",
    )

    first = ai_router.analyze_bundle(BUNDLE)

    assert first["status"] == "MANUAL_REVIEW_BUDGET"
    assert first["resume_state"]["next_tier"] == "REASONING"
    assert calls == ["FAST"]

    # Tahap kedua: lanjutkan hanya REASONING.
    monkeypatch.setattr(
        ai_router,
        "can_use_expensive_tier",
        lambda tier, messages: True,
    )

    second = ai_router.analyze_bundle(
        BUNDLE,
        resume_state=first["resume_state"],
    )

    assert second["status"] == "ANALYZED"
    assert calls == ["FAST", "REASONING"]
    assert [
        item["tier"] for item in second["attempts"]
    ] == ["FAST", "REASONING"]
   
    
def test_metadata_klasifikasi_baru_diteruskan_ke_ai():
    bundle = {
        "target_message_id": 456,
        "messages": [
            {
                "telegram_message_id": 456,
                "text": "Try sell",
                "message_role": "ENTRY_REQUEST",
                "message_roles": ["ENTRY_REQUEST"],
                "trade_episode_id": None,
                "trade_episode_status": "WAITING_FOR_ZONE",
                "media": [],
                "edit_history": [],
            },
            {
                "telegram_message_id": 457,
                "text": "Ntar ku carikan re entry",
                "message_role": "REENTRY_PLAN",
                "message_roles": ["REENTRY_PLAN"],
                "trade_episode_id": None,
                "trade_episode_status": None,
                "media": [],
                "edit_history": [],
            },
            {
                "telegram_message_id": 458,
                "text": "4043-4044",
                "message_role": "PRICE_ZONE",
                "message_roles": ["PRICE_ZONE"],
                "trade_episode_id": "trade_1",
                "trade_episode_status": "ACTIVE",
                "media": [],
                "edit_history": [],
            },
            {
                "telegram_message_id": 459,
                "text": "Skip",
                "message_role": "SKIP",
                "message_roles": ["SKIP"],
                "trade_episode_id": "trade_1",
                "trade_episode_status": "SKIP_REQUESTED",
                "media": [],
                "edit_history": [],
            },
        ],
    }

    prepared = ai_router.prepare_bundle(bundle)

    assert len(prepared["messages"]) == 4

    expected = [
        ("ENTRY_REQUEST", "WAITING_FOR_ZONE"),
        ("REENTRY_PLAN", None),
        ("PRICE_ZONE", "ACTIVE"),
        ("SKIP", "SKIP_REQUESTED"),
    ]

    for message, (role, status) in zip(
        prepared["messages"],
        expected,
    ):
        assert message["message_role"] == role
        assert message["message_roles"] == [role]
        assert message.get("trade_episode_status") == status

    payload = json.loads(
        ai_router.build_messages(
            bundle,
            stage="METADATA_TEST",
        )[1]["content"]
    )

    assert payload["bundle"] == prepared







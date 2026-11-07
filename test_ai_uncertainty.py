from app.ai.ai_router import _final_analysis_status


def test_uncertainties_memaksa_manual_review():
    analysis = {
        "status": "CLEAR",
        "confidence": 0.95,
        "needs_reasoning": False,
        "needs_escalation": False,
        "critical_ambiguity": False,
        "uncertainties": [
            "Tidak ada informasi take profit.",
            "Stop loss 20/30 pips ambigu.",
        ],
    }

    assert _final_analysis_status(analysis) == "MANUAL_REVIEW"

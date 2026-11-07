from pathlib import Path
from datetime import datetime

TARGET = Path('app/ai/ai_router.py')

NEW_ROUTER_TAIL = r'''def _is_model_refusal(content):
    """Deteksi refusal eksplisit di awal respons, sebelum parsing JSON."""

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
    """Membuat ringkasan pemanggilan tanpa menyimpan payload sensitif."""

    analysis = response.get("analysis") or {}

    return {
        "tier": tier,
        "model": analysis.get("model"),
        "cost_rp": str(analysis.get("cost_rp", "0")),
        "usage": analysis.get("usage") or {},
    }


def _refused_result(tier, content, attempts, **extra):
    """Hasil proses REFUSED; bukan status JSON dari model."""

    return {
        "status": "REFUSED",
        "reason": "MODEL_EXPLICIT_REFUSAL",
        "refused_tier": tier,
        "refusal_excerpt": (content or "")[:600],
        "attempts": attempts,
        **extra,
    }


def _final_analysis_status(analysis):
    """Jangan menandai hasil yang masih meragukan sebagai ANALYZED."""

    if analysis.get("status") == "NO_SIGNAL":
        return "NO_SIGNAL"

    needs_manual_review = (
        analysis.get("status") in {"NEEDS_REASONING", "UNRESOLVED"}
        or analysis.get("needs_reasoning") is True
        or analysis.get("needs_escalation") is True
        or analysis.get("critical_ambiguity") is True
        or analysis.get("confidence", 0.0) < 0.80
    )

    return "MANUAL_REVIEW" if needs_manual_review else "ANALYZED"


def analyze_bundle(bundle):
    """
    Routing SIREADER:
    - Haiku menjadi percobaan pertama.
    - Jika Haiku refusal eksplisit, Sonnet dicoba SATU KALI.
    - Refusal tidak diteruskan ke Opus.
    - Jalur reasoning biasa tetap dapat memakai Opus hanya jika
      Sonnet menghasilkan UNRESOLVED + critical_ambiguity + needs_escalation.
    - Hasil NEEDS_REASONING/UNRESOLVED tidak ditandai ANALYZED.
    """

    prepared = prepare_bundle(bundle)
    attempts = []

    # --------------------------------------------------------
    # LEVEL 1: HAIKU
    # --------------------------------------------------------

    fast_messages = build_messages(
        bundle,
        stage="FAST_INITIAL_ANALYSIS",
    )

    try:
        fast_response = _run_tier("FAST", fast_messages)
    except BudgetExceededError:
        return {
            "status": "BLOCKED_HARD_BUDGET",
            "attempts": attempts,
        }

    fast_content = fast_response["analysis"].get("content", "")
    attempts.append(_attempt_record("FAST", fast_response))

    # Refusal Haiku: fallback Sonnet maksimal satu kali.
    if _is_model_refusal(fast_content):
        sonnet_messages = build_messages(
            bundle,
            stage="REFUSAL_FALLBACK",
        )

        if not can_use_expensive_tier("REASONING", sonnet_messages):
            return {
                "status": "MANUAL_REVIEW_BUDGET",
                "reason": "FAST_REFUSED_SONNET_FALLBACK_BLOCKED_BY_SOFT_BUDGET",
                "fast_refusal_excerpt": fast_content[:600],
                "attempts": attempts,
            }

        try:
            sonnet_response = _run_tier("REASONING", sonnet_messages)
        except BudgetExceededError:
            return {
                "status": "MANUAL_REVIEW_BUDGET",
                "reason": "FAST_REFUSED_SONNET_FALLBACK_BLOCKED_BY_HARD_BUDGET",
                "fast_refusal_excerpt": fast_content[:600],
                "attempts": attempts,
            }

        sonnet_content = sonnet_response["analysis"].get("content", "")
        attempts.append(_attempt_record("REASONING", sonnet_response))

        if _is_model_refusal(sonnet_content):
            return _refused_result(
                "REASONING",
                sonnet_content,
                attempts,
                previous_refusal_tier="FAST",
            )

        sonnet = parse_model_output(sonnet_content, prepared)
        return {
            "status": _final_analysis_status(sonnet),
            "final_analysis": sonnet,
            "attempts": attempts,
            "fallback_from": "FAST_REFUSAL",
            "opus_skipped_after_refusal_fallback": True,
        }

    # Tidak ada refusal: proses normal seperti sebelumnya.
    fast = parse_model_output(fast_content, prepared)

    if fast["status"] == "NO_SIGNAL":
        return {
            "status": "NO_SIGNAL",
            "final_analysis": fast,
            "attempts": attempts,
        }

    needs_reasoning = (
        fast["needs_reasoning"]
        or fast["status"] in {"NEEDS_REASONING", "UNRESOLVED"}
        or fast["confidence"] < 0.80
    )

    if not needs_reasoning:
        return {
            "status": _final_analysis_status(fast),
            "final_analysis": fast,
            "attempts": attempts,
        }

    # --------------------------------------------------------
    # LEVEL 2: SONNET — jalur reasoning normal
    # --------------------------------------------------------

    sonnet_messages = build_messages(
        bundle,
        stage="REASONING_REVIEW",
        previous_analysis=fast,
    )

    if not can_use_expensive_tier("REASONING", sonnet_messages):
        return {
            "status": "MANUAL_REVIEW_BUDGET",
            "final_analysis": fast,
            "attempts": attempts,
        }

    try:
        sonnet_response = _run_tier("REASONING", sonnet_messages)
    except BudgetExceededError:
        return {
            "status": "MANUAL_REVIEW_BUDGET",
            "final_analysis": fast,
            "attempts": attempts,
        }

    sonnet_content = sonnet_response["analysis"].get("content", "")
    attempts.append(_attempt_record("REASONING", sonnet_response))

    if _is_model_refusal(sonnet_content):
        return _refused_result("REASONING", sonnet_content, attempts)

    sonnet = parse_model_output(sonnet_content, prepared)

    # Opus hanya untuk ambiguitas kritis yang dinyatakan Sonnet,
    # bukan untuk mengulang atau mengatasi refusal.
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

    # --------------------------------------------------------
    # LEVEL 3: OPUS — hanya ambiguitas kritis
    # --------------------------------------------------------

    opus_messages = build_messages(
        bundle,
        stage="CRITICAL_AMBIGUITY_REVIEW",
        previous_analysis=sonnet,
    )

    if not can_use_expensive_tier("ESCALATION", opus_messages):
        return {
            "status": "MANUAL_REVIEW_BUDGET",
            "final_analysis": sonnet,
            "attempts": attempts,
        }

    try:
        opus_response = _run_tier("ESCALATION", opus_messages)
    except BudgetExceededError:
        return {
            "status": "MANUAL_REVIEW_BUDGET",
            "final_analysis": sonnet,
            "attempts": attempts,
        }

    opus_content = opus_response["analysis"].get("content", "")
    attempts.append(_attempt_record("ESCALATION", opus_response))

    if _is_model_refusal(opus_content):
        return _refused_result("ESCALATION", opus_content, attempts)

    opus = parse_model_output(opus_content, prepared)

    return {
        "status": _final_analysis_status(opus),
        "final_analysis": opus,
        "attempts": attempts,
    }
'''


def main():
    if not TARGET.exists():
        raise SystemExit(f"File tidak ditemukan: {TARGET}")

    source = TARGET.read_text(encoding="utf-8")
    marker = "def analyze_bundle(bundle):"
    if source.count(marker) != 1:
        raise SystemExit(
            "Patch dihentikan: marker def analyze_bundle(bundle): "
            f"ditemukan {source.count(marker)} kali, seharusnya tepat 1. "
            "Tidak ada file yang diubah."
        )

    if "def _is_model_refusal(content):" in source:
        raise SystemExit("Patch tampaknya sudah diterapkan. Tidak ada file yang diubah.")

    if "def _run_tier(tier, messages):" not in source:
        raise SystemExit("Patch dihentikan: fungsi _run_tier tidak ditemukan. Tidak ada file yang diubah.")

    backup = TARGET.with_name(
        f"ai_router.py.bak_refusal_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    backup.write_text(source, encoding="utf-8")

    prefix = source[:source.index(marker)]
    updated = prefix + NEW_ROUTER_TAIL
    TARGET.write_text(updated, encoding="utf-8")

    print("PATCH BERHASIL DITERAPKAN")
    print(f"File diperbarui : {TARGET}")
    print(f"Backup dibuat   : {backup}")
    print("Parser, schema, prepare_bundle, build_messages, Cost Guard, dan _run_tier dipertahankan.")
    print("Pipeline/database tidak diubah.")
    print("Refusal Haiku -> fallback Sonnet maksimal 1 kali; tidak ke Opus.")


if __name__ == "__main__":
    main()

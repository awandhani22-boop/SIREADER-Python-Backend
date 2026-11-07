"""One-shot Sonnet output-limit test for SIREADER target message 20519.

Uses the current SYSTEM_PROMPT and the same stored Telegram bundle.
Does not touch AIAnalysisLog, does not retry the pipeline, and makes
at most one paid API call. The output limit is raised to 1200 tokens.
"""

import json
import sys
from datetime import datetime
from decimal import Decimal

from app.ai.ai_router import build_messages, parse_model_output, prepare_bundle
from app.ai.cost_guard import (
    BudgetExceededError,
    estimate_request_cost,
    get_daily_usage,
)
from app.ai.message_aggregator import collect_message_bundle
from app.ai.omniroute_client import call_ai
from app.config import settings


CHAT_ID = -1003014486607
TARGET_MESSAGE_ID = 20519
TIER = "REASONING"
MAX_OUTPUT_TOKENS = 1200


def is_obvious_refusal(content: str) -> bool:
    sample = content.strip().lower()
    markers = (
        "i can't help with this request",
        "i cannot help with this request",
        "i'm not able to assist",
        "i am not able to assist",
        "i can't assist with",
        "i cannot assist with",
    )
    return any(marker in sample[:1000] for marker in markers)


def soft_budget_allows(messages: list) -> tuple[bool, dict, dict]:
    serialized = json.dumps(
        messages,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    estimate = estimate_request_cost(
        tier=TIER,
        prompt_text=serialized,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )
    daily = get_daily_usage()
    projected = Decimal(str(daily["projected_cost_rp"]))
    reserved = Decimal(str(estimate["reserved_cost_rp"]))
    target = Decimal(str(settings.AI_TARGET_DAILY_BUDGET_RP))
    return projected + reserved <= target, estimate, daily


def main() -> int:
    print("SIREADER — SONNET OUTPUT-LIMIT TEST")
    print("Read-only bundle; no AIAnalysisLog changes; no pipeline retry.")
    print(f"Chat ID: {CHAT_ID}")
    print(f"Telegram target message ID: {TARGET_MESSAGE_ID}")
    print(f"Tier: {TIER}")
    print(f"Max output tokens: {MAX_OUTPUT_TOKENS}")

    bundle = collect_message_bundle(
        chat_id=CHAT_ID,
        telegram_message_id=TARGET_MESSAGE_ID,
        window_minutes=10,
        max_messages=15,
    )
    if not bundle:
        print("RESULT: TARGET_NOT_FOUND")
        return 2

    if not any((m.get("text") or "").strip() for m in bundle.get("messages", [])):
        print("RESULT: NO_TEXT_IN_BUNDLE")
        return 2

    prepared = prepare_bundle(bundle)
    messages = build_messages(
        bundle,
        stage="MANUAL_SONNET_OUTPUT_LIMIT_TEST_20519",
    )
    allowed, estimate, daily_before = soft_budget_allows(messages)
    print("Bundle message count:", len(prepared["messages"]))
    print("Estimated reservation Rp:", estimate.get("reserved_cost_rp"))
    print("Daily projected cost before Rp:", daily_before.get("projected_cost_rp"))
    print("Daily soft target Rp:", settings.AI_TARGET_DAILY_BUDGET_RP)

    if not allowed:
        print("RESULT: BLOCKED_SOFT_DAILY_TARGET")
        return 3

    print("Started:", datetime.now().astimezone().isoformat(timespec="seconds"))
    try:
        response = call_ai(
            tier=TIER,
            messages=messages,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
    except BudgetExceededError as exc:
        print("RESULT: BLOCKED_HARD_COST_GUARD")
        print("DETAIL:", str(exc))
        return 4
    except Exception as exc:
        print("RESULT: REQUEST_ERROR")
        print("ERROR TYPE:", type(exc).__name__)
        print("DETAIL:", str(exc))
        return 5

    content = response.get("content")
    print("MODEL:", response.get("model"))
    print("COST_RP:", response.get("cost_rp"))
    print("USAGE:", json.dumps(response.get("usage"), ensure_ascii=False, default=str))

    if not isinstance(content, str):
        print("RESULT: INVALID_RESPONSE_SHAPE")
        print("RESPONSE KEYS:", list(response.keys()))
    elif is_obvious_refusal(content):
        print("RESULT: REFUSED")
        print("RAW RESPONSE:\n", content[:6000])
    else:
        try:
            parsed = parse_model_output(content, prepared)
        except Exception as exc:
            print("RESULT: OUTPUT_OR_SCHEMA_VALIDATION_ERROR")
            print("ERROR TYPE:", type(exc).__name__)
            print("DETAIL:", str(exc))
            print("RAW RESPONSE:\n", content[:6000])
        else:
            print("RESULT: PARSER_ACCEPTED")
            print("PARSED JSON:\n", json.dumps(parsed, ensure_ascii=False, indent=2, default=str))

    try:
        print("\nDAILY COST GUARD SNAPSHOT AFTER:")
        print(json.dumps(get_daily_usage(), ensure_ascii=False, indent=2, default=str))
    except Exception as exc:
        print("Could not read daily cost snapshot:", str(exc))

    print("\nDone. AIAnalysisLog was not created or modified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

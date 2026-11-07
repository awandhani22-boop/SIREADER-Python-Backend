import json

from app.ai.message_aggregator import collect_message_bundle
from app.ai.ai_router import build_messages
from app.ai.cost_guard import estimate_request_cost, get_tier_config

chat_id = -1003014486607
target_id = 20519

bundle = collect_message_bundle(
    chat_id,
    target_id,
    window_minutes=10,
    max_messages=15,
)

if not bundle:
    raise SystemExit("Pesan target tidak ditemukan.")

print("Jumlah pesan konteks:", len(bundle["messages"]))

for tier in ("FAST", "REASONING", "ESCALATION"):
    messages = build_messages(
        bundle,
        stage="COST_ESTIMATION",
    )

    prompt = json.dumps(
        messages,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    config = get_tier_config(tier)

    estimate = estimate_request_cost(
        tier=tier,
        prompt_text=prompt,
        max_output_tokens=config["max_output"],
    )

    print("\nMODEL:", config["model"])
    print("Estimasi input:", estimate["estimated_input_tokens"])
    print("Maksimum output:", estimate["reserved_output_tokens"])
    print("Estimasi biaya:", estimate["estimated_cost_rp"])
    print("Reservasi:", estimate["reserved_cost_rp"])


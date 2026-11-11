from __future__ import annotations

from app.workers.policy import (
    POLICY_VERSION,
    SensitiveTaskAction,
    evaluate_workforce_policy,
)
from app.workers.registry import build_default_registry


def evaluate(
    worker_id: str,
    capability: str,
    task_type: str,
    *,
    title: str = "Synthetic task",
    payload=None,
    mode: str = "MOCK",
    live_enabled: bool = False,
):
    worker = build_default_registry()[worker_id]
    return evaluate_workforce_policy(
        worker=worker,
        task_type=task_type,
        required_capability=capability,
        title=title,
        input_payload=payload or {"text": "Synthetic non-sensitive content."},
        provider_mode=mode,
        live_provider_enabled=live_enabled,
    )


def test_mock_policy_allows_matching_haiku_capability_with_fast_tier() -> None:
    decision = evaluate("haiku", "quick_research", "QUICK_RESEARCH")
    assert decision.policy_version == POLICY_VERSION
    assert decision.action is SensitiveTaskAction.ALLOW
    assert decision.reason_code == "POLICY_ALLOWED"
    assert decision.model_tier == "FAST"
    assert decision.requires_human_review is False


def test_unknown_capability_is_rejected_and_payload_is_not_forwarded() -> None:
    decision = evaluate("haiku", "unregistered_capability", "QUICK_RESEARCH")
    assert decision.action is SensitiveTaskAction.REJECT
    assert decision.reason_code == "CAPABILITY_UNKNOWN"
    assert decision.model_tier == "NONE"
    assert decision.sanitized_input == {}


def test_capability_worker_or_task_mismatch_is_rejected() -> None:
    decision = evaluate("haiku", "structured_reasoning", "QUICK_RESEARCH")
    assert decision.action is SensitiveTaskAction.REJECT
    assert decision.reason_code == "CAPABILITY_TASK_MISMATCH"


def test_personal_data_is_redacted_and_sent_to_human_review() -> None:
    decision = evaluate(
        "haiku",
        "quick_research",
        "QUICK_RESEARCH",
        title="Review contact details",
        payload={"email": "person@example.test", "text": "A harmless summary."},
    )
    assert decision.action is SensitiveTaskAction.REVIEW
    assert decision.reason_code == "SENSITIVE_PERSONAL_DATA_REVIEW"
    assert decision.requires_human_review is True
    assert decision.sanitized_input["email"] == "[REDACTED_PERSONAL_DATA]"
    assert decision.redacted_fields == 1


def test_explicit_trade_execution_instruction_is_rejected() -> None:
    decision = evaluate(
        "haiku",
        "quick_research",
        "QUICK_RESEARCH",
        title="Place a live trade now",
    )
    assert decision.action is SensitiveTaskAction.REJECT
    assert decision.reason_code == "SENSITIVE_TASK_REJECTED"
    assert decision.sanitized_input == {}


def test_private_key_material_is_rejected() -> None:
    decision = evaluate(
        "haiku",
        "quick_research",
        "QUICK_RESEARCH",
        payload={"document": "-----BEGIN PRIVATE KEY----- synthetic-material"},
    )
    assert decision.action is SensitiveTaskAction.REJECT
    assert decision.reason_code == "SENSITIVE_TASK_REJECTED"


def test_unknown_provider_mode_is_rejected() -> None:
    decision = evaluate("haiku", "quick_research", "QUICK_RESEARCH", mode="AUTO")
    assert decision.action is SensitiveTaskAction.REJECT
    assert decision.reason_code == "UNKNOWN_PROVIDER_MODE"


def test_python_worker_is_allowed_in_hybrid_mode_without_live_ai() -> None:
    decision = evaluate(
        "decision_engineer",
        "rule_validation",
        "RULE_VALIDATION",
        mode="ANTHROPIC",
        live_enabled=False,
    )
    assert decision.action is SensitiveTaskAction.ALLOW
    assert decision.model_tier == "NONE"


def test_ai_worker_is_blocked_when_paid_inference_is_off() -> None:
    decision = evaluate(
        "haiku", "quick_research", "QUICK_RESEARCH",
        mode="ANTHROPIC", live_enabled=False,
    )
    assert decision.action is SensitiveTaskAction.REJECT
    assert decision.reason_code == "LIVE_PROVIDER_DISABLED"


def test_ai_worker_is_allowed_only_with_matching_profile_and_live_gate() -> None:
    decision = evaluate(
        "haiku", "quick_research", "QUICK_RESEARCH",
        mode="ANTHROPIC", live_enabled=True,
    )
    assert decision.action is SensitiveTaskAction.ALLOW
    assert decision.model_tier == "FAST"


def test_model_tier_above_worker_ceiling_is_rejected() -> None:
    workers = build_default_registry()
    altered_haiku = workers["haiku"].model_copy(update={
        "capability_ids": ["quick_research", "complex_reasoning"],
    })
    decision = evaluate_workforce_policy(
        worker=altered_haiku,
        task_type="COMPLEX_REASONING",
        required_capability="complex_reasoning",
        title="Synthetic task",
        input_payload={"text": "Synthetic content."},
        provider_mode="ANTHROPIC",
        live_provider_enabled=True,
    )
    assert decision.action is SensitiveTaskAction.REJECT
    assert decision.reason_code == "MODEL_TIER_POLICY_REJECTED"


def test_trading_capability_cannot_be_routed_to_ai_worker() -> None:
    workers = build_default_registry()
    altered_haiku = workers["haiku"].model_copy(update={
        "capability_ids": ["quick_research", "decision_evaluation"],
    })
    decision = evaluate_workforce_policy(
        worker=altered_haiku,
        task_type="DECISION_EVALUATION",
        required_capability="decision_evaluation",
        title="Synthetic non-execution evaluation",
        input_payload={"facts": {"safe": True}},
        provider_mode="ANTHROPIC",
        live_provider_enabled=True,
    )
    assert decision.action is SensitiveTaskAction.REJECT
    assert decision.reason_code == "TRADING_CAPABILITY_PROVIDER_FORBIDDEN"

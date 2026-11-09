"""Backend-owned deterministic policy for generic workforce model routing and sensitive inputs.

This module is deliberately isolated from Telegram trading AI and execution paths.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.ai.sanitizer import REDACTED, mask_string, sanitize_review_payload
from app.governance.review_policy import TRADING_RELATED_CAPABILITIES
from app.workers.registry import CAPABILITY_CATALOG
from app.workers.runtime_contract import WorkerContract

POLICY_VERSION = "2026.11.10"

class SensitiveTaskAction(StrEnum):
    ALLOW = "ALLOW"
    REDACT = "REDACT"
    REVIEW = "REVIEW"
    REJECT = "REJECT"

PROVIDER_WORKERS = frozenset({"haiku", "sonnet", "opus"})
PROVIDER_FORBIDDEN_WORKERS = frozenset({"decision_engineer", "market_monitor"})
WORKER_TIER_CEILINGS = {
    "haiku": "FAST",
    "sonnet": "REASONING",
    "opus": "ESCALATION",
}
TIER_RANK = {"NONE": 0, "FAST": 1, "REASONING": 2, "ESCALATION": 3}

CAPABILITY_MODEL_TIERS = {
    "quick_research": "FAST",
    "information_extraction": "FAST",
    "summarization": "FAST",
    "lightweight_classification": "FAST",
    "data_analysis": "REASONING",
    "comparison": "REASONING",
    "evaluation": "REASONING",
    "structured_reasoning": "REASONING",
    "complex_reasoning": "ESCALATION",
    "strategic_analysis": "ESCALATION",
    "deep_review": "ESCALATION",
    "decision_evaluation": "ESCALATION",
}

PII_KEY_MARKERS = (
    "email", "phone", "mobile", "passport", "national_id",
    "personal_id", "identity_number", "date_of_birth", "home_address",
)
FORBIDDEN_EXECUTION_KEYS = frozenset({
    "place_order", "execute_trade", "order_execution", "broker_command",
    "mt5_order", "hfm_order", "close_position", "modify_position",
})
EMAIL_PATTERN = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
LIVE_EXECUTION_PATTERN = re.compile(
    r"(?i)\b(?:place|submit|send|execute)\s+(?:a\s+)?(?:live\s+)?(?:trade|order)\b"
    r"|\b(?:mt5|hfm)\s+(?:order|execution|broker)\b"
)

@dataclass(frozen=True)
class WorkforcePolicyDecision:
    policy_version: str
    action: SensitiveTaskAction
    reason_code: str
    model_tier: str
    sanitized_title: str
    sanitized_input: dict[str, Any]
    requires_human_review: bool
    redacted_fields: int

class WorkforcePolicyError(ValueError):
    pass

def _redact_personal_data(value: Any, *, path: str = "") -> tuple[Any, int]:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        count = 0
        for key, nested in value.items():
            key_text = str(key).lower()
            if any(marker in key_text for marker in PII_KEY_MARKERS):
                output[key] = "[REDACTED_PERSONAL_DATA]"
                count += 1
            else:
                output[key], found = _redact_personal_data(nested, path=f"{path}.{key}")
                count += found
        return output, count
    if isinstance(value, list):
        output = []
        count = 0
        for nested in value:
            sanitized, found = _redact_personal_data(nested, path=path)
            output.append(sanitized)
            count += found
        return output, count
    if isinstance(value, str):
        masked, count = EMAIL_PATTERN.subn("[REDACTED_PERSONAL_DATA]", value)
        return masked, count
    return value, 0

def _contains_forbidden_execution(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_").replace(" ", "_")
            if normalized in FORBIDDEN_EXECUTION_KEYS or _contains_forbidden_execution(nested):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_forbidden_execution(item) for item in value)
    if isinstance(value, str):
        return bool(PRIVATE_KEY_PATTERN.search(value) or LIVE_EXECUTION_PATTERN.search(value))
    return False

def evaluate_workforce_policy(
    *,
    worker: WorkerContract,
    task_type: str,
    required_capability: str,
    title: str,
    input_payload: dict[str, Any],
    provider_mode: str,
) -> WorkforcePolicyDecision:
    """Validate capability, choose one deterministic tier, and scrub sensitive input.

    No provider retry/escalation occurs here. The selected tier is fixed before the
    attempt's single provider request so UNKNOWN outcomes are never replayed elsewhere.
    """
    mode = str(provider_mode).strip().upper()
    live_mode = mode == "OMNIROUTE"
    if required_capability not in CAPABILITY_CATALOG:
        return WorkforcePolicyDecision(
            POLICY_VERSION, SensitiveTaskAction.REJECT, "CAPABILITY_UNKNOWN", "NONE",
            "[REDACTED]", {}, False, 0,
        )
    capability = CAPABILITY_CATALOG[required_capability]
    if required_capability not in worker.capability_ids or task_type not in capability.task_types:
        return WorkforcePolicyDecision(
            POLICY_VERSION, SensitiveTaskAction.REJECT, "CAPABILITY_TASK_MISMATCH", "NONE",
            "[REDACTED]", {}, False, 0,
        )

    if _contains_forbidden_execution({"title": title, "input": input_payload}):
        return WorkforcePolicyDecision(
            POLICY_VERSION, SensitiveTaskAction.REJECT, "SENSITIVE_TASK_REJECTED", "NONE",
            "[REDACTED]", {}, False, 0,
        )

    selected_tier = CAPABILITY_MODEL_TIERS.get(required_capability, "NONE")
    if live_mode:
        if worker.worker_id not in PROVIDER_WORKERS or worker.worker_id in PROVIDER_FORBIDDEN_WORKERS:
            return WorkforcePolicyDecision(
                POLICY_VERSION, SensitiveTaskAction.REJECT, "LIVE_PROVIDER_WORKER_FORBIDDEN", "NONE",
                "[REDACTED]", {}, False, 0,
            )
        if required_capability in TRADING_RELATED_CAPABILITIES:
            return WorkforcePolicyDecision(
                POLICY_VERSION, SensitiveTaskAction.REJECT, "TRADING_CAPABILITY_PROVIDER_FORBIDDEN", "NONE",
                "[REDACTED]", {}, False, 0,
            )
        ceiling = WORKER_TIER_CEILINGS[worker.worker_id]
        if selected_tier == "NONE" or TIER_RANK[selected_tier] > TIER_RANK[ceiling] or worker.model_tier != ceiling:
            return WorkforcePolicyDecision(
                POLICY_VERSION, SensitiveTaskAction.REJECT, "MODEL_TIER_POLICY_REJECTED", "NONE",
                "[REDACTED]", {}, False, 0,
            )
    elif mode != "MOCK":
        return WorkforcePolicyDecision(
            POLICY_VERSION, SensitiveTaskAction.REJECT, "UNKNOWN_PROVIDER_MODE", "NONE",
            "[REDACTED]", {}, False, 0,
        )

    secret_scrubbed = sanitize_review_payload(input_payload)
    scrubbed_title = mask_string(title)
    personal_scrubbed, personal_count = _redact_personal_data(secret_scrubbed)
    scrubbed_title, title_pii_count = EMAIL_PATTERN.subn("[REDACTED_PERSONAL_DATA]", scrubbed_title)
    redactions = personal_count + title_pii_count
    secrets_changed = secret_scrubbed != input_payload or scrubbed_title != mask_string(title)

    if redactions:
        action = SensitiveTaskAction.REVIEW
        reason = "SENSITIVE_PERSONAL_DATA_REVIEW"
    elif secrets_changed:
        action = SensitiveTaskAction.REDACT
        reason = "CREDENTIALS_REDACTED"
    else:
        action = SensitiveTaskAction.ALLOW
        reason = "POLICY_ALLOWED"

    return WorkforcePolicyDecision(
        policy_version=POLICY_VERSION,
        action=action,
        reason_code=reason,
        model_tier=selected_tier,
        sanitized_title=scrubbed_title,
        sanitized_input=personal_scrubbed,
        requires_human_review=action is SensitiveTaskAction.REVIEW,
        redacted_fields=redactions,
    )

"""Versioned, backend-owned review policy specification for SIREADER (Phase 5.2.2).

Authoritative governance rulebook for human output review eligibility and sampling.
Client-supplied flags (e.g. requires_human_review) are completely ignored.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from app.workers.registry import CAPABILITY_CATALOG
from app.workers.runtime_contract import CapabilityContract

POLICY_VERSION = "2026.11.01"


class ReviewTriggerMode(StrEnum):
    MANDATORY = "MANDATORY"
    SAMPLE = "SAMPLE"
    NONE = "NONE"


@dataclass(frozen=True)
class CapabilityReviewRule:
    mode: ReviewTriggerMode
    sample_rate_percent: int = 0
    sample_salt: str = "sireader_governance_v1"


# Exhaustive mapping for all non-trading review capabilities in CAPABILITY_CATALOG
REVIEW_POLICY_CATALOG: dict[tuple[str, str], CapabilityReviewRule] = {
    # Haiku research capabilities
    ("quick_research", "QUICK_RESEARCH"): CapabilityReviewRule(ReviewTriggerMode.SAMPLE, 20),
    ("quick_research", "RESEARCH"): CapabilityReviewRule(ReviewTriggerMode.SAMPLE, 20),
    ("information_extraction", "INFORMATION_EXTRACTION"): CapabilityReviewRule(ReviewTriggerMode.SAMPLE, 10),
    ("information_extraction", "CLASSIFICATION"): CapabilityReviewRule(ReviewTriggerMode.SAMPLE, 10),
    ("summarization", "SUMMARIZATION"): CapabilityReviewRule(ReviewTriggerMode.SAMPLE, 15),
    ("summarization", "CLASSIFICATION"): CapabilityReviewRule(ReviewTriggerMode.SAMPLE, 15),

    # Sonnet analysis capabilities
    ("data_analysis", "DATA_ANALYSIS"): CapabilityReviewRule(ReviewTriggerMode.SAMPLE, 25),
    ("data_analysis", "REASONING"): CapabilityReviewRule(ReviewTriggerMode.SAMPLE, 25),
    ("comparison", "COMPARISON"): CapabilityReviewRule(ReviewTriggerMode.SAMPLE, 20),
    ("comparison", "REASONING"): CapabilityReviewRule(ReviewTriggerMode.SAMPLE, 20),
    ("evaluation", "EVALUATION"): CapabilityReviewRule(ReviewTriggerMode.MANDATORY),
    ("evaluation", "REASONING"): CapabilityReviewRule(ReviewTriggerMode.MANDATORY),
    ("structured_reasoning", "STRUCTURED_REASONING"): CapabilityReviewRule(ReviewTriggerMode.SAMPLE, 30),
    ("structured_reasoning", "REASONING"): CapabilityReviewRule(ReviewTriggerMode.SAMPLE, 30),

    # Opus escalation capabilities
    ("complex_reasoning", "COMPLEX_REASONING"): CapabilityReviewRule(ReviewTriggerMode.MANDATORY),
    ("complex_reasoning", "ESCALATION"): CapabilityReviewRule(ReviewTriggerMode.MANDATORY),
    ("strategic_analysis", "STRATEGIC_ANALYSIS"): CapabilityReviewRule(ReviewTriggerMode.MANDATORY),
    ("strategic_analysis", "ESCALATION"): CapabilityReviewRule(ReviewTriggerMode.MANDATORY),
    ("deep_review", "DEEP_REVIEW"): CapabilityReviewRule(ReviewTriggerMode.MANDATORY),
    ("deep_review", "ESCALATION"): CapabilityReviewRule(ReviewTriggerMode.MANDATORY),

    # Data archiving
    ("data_archiving", "DATA_ARCHIVING"): CapabilityReviewRule(ReviewTriggerMode.NONE),
    ("data_archiving", "ARCHIVAL"): CapabilityReviewRule(ReviewTriggerMode.NONE),
}

# Explicit hard ban: trading operations are strictly decoupled from human output review
TRADING_RELATED_CAPABILITIES: frozenset[str] = frozenset({
    "rule_validation",
    "decision_evaluation",
    "risk_assessment_simulation",
    "decision_consistency_checks",
    "market_monitoring_simulation",
    "market_condition_detection",
    "anomaly_detection_simulation",
    "market_event_reporting",
})


def evaluate_review_eligibility(
    capability_id: str,
    task_type: str,
    task_id: str,
    catalog: Mapping[str, CapabilityContract] | None = None,
) -> bool:
    """
    Evaluates whether a completed non-trading task requires human output review.

    Invariants:
    1. Trading-related capabilities always return False (hard ban).
    2. Capabilities not in authoritative catalog return False.
    3. Task types not in capability contract return False.
    4. Unmapped pairs fail-closed (False).
    5. Sampling is deterministic and reproducible for a given task_id and policy_version.
    """
    if capability_id in TRADING_RELATED_CAPABILITIES:
        return False

    cat = catalog or CAPABILITY_CATALOG
    contract = cat.get(capability_id)
    if not contract or not contract.enabled:
        return False

    if task_type not in contract.task_types:
        return False

    rule = REVIEW_POLICY_CATALOG.get((capability_id, task_type))
    if not rule or rule.mode == ReviewTriggerMode.NONE:
        return False

    if rule.mode == ReviewTriggerMode.MANDATORY:
        return True

    if rule.mode == ReviewTriggerMode.SAMPLE:
        seed = f"{POLICY_VERSION}:{task_id}:{rule.sample_salt}"
        bucket = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16) % 100
        return bucket < rule.sample_rate_percent

    return False

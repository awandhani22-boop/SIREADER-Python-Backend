"""Central, versioned execution profiles for the seven logical SIREADER workers.

A worker identity is not a permanently running model. Four profiles execute
deterministic Python handlers; only Haiku/Sonnet/Opus profiles may request AI.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ExecutionMode = Literal["PYTHON", "AI"]


@dataclass(frozen=True)
class WorkerProfile:
    worker_id: str
    execution_mode: ExecutionMode
    model_tier: str
    max_output_tokens: int
    ai_enabled_by_default: bool
    description: str


WORKER_PROFILES: dict[str, WorkerProfile] = {
    "supervisor": WorkerProfile(
        "supervisor", "PYTHON", "NONE", 0, False,
        "Deterministic intake, routing, task decomposition and oversight; AI assist remains opt-in and off.",
    ),
    "haiku": WorkerProfile(
        "haiku", "AI", "FAST", 700, True,
        "Fast bounded extraction, classification, summarization and research.",
    ),
    "sonnet": WorkerProfile(
        "sonnet", "AI", "REASONING", 1400, True,
        "Structured reasoning, comparison and deeper analysis.",
    ),
    "opus": WorkerProfile(
        "opus", "AI", "ESCALATION", 1800, True,
        "Explicitly escalated complex reasoning and independent deep review.",
    ),
    "decision_engineer": WorkerProfile(
        "decision_engineer", "PYTHON", "NONE", 0, False,
        "Deterministic rule validation and hypothetical risk checks only.",
    ),
    "market_monitor": WorkerProfile(
        "market_monitor", "PYTHON", "NONE", 0, False,
        "Read-only analysis of supplied market observations; no market feed or trading actions.",
    ),
    "data_archivist": WorkerProfile(
        "data_archivist", "PYTHON", "NONE", 0, False,
        "Deterministic archive manifests, fingerprints and metadata organization.",
    ),
}


def profile_for_worker(worker_id: str) -> WorkerProfile:
    try:
        return WORKER_PROFILES[worker_id]
    except KeyError as exc:
        raise ValueError(f"Unknown SIREADER worker profile: {worker_id!r}") from exc


def validate_worker_profile(worker_id: str, model_tier: str) -> WorkerProfile:
    profile = profile_for_worker(worker_id)
    if model_tier != profile.model_tier:
        raise ValueError(
            f"Worker profile tier mismatch for {worker_id}: "
            f"registry={model_tier}, profile={profile.model_tier}"
        )
    return profile

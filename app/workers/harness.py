"""Central control-plane routing for all seven SIREADER worker profiles.

This module is the single place that decides whether a worker task is MOCK,
deterministic Python, or an explicitly enabled Anthropic model request.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.ai.cost_guard import get_tier_config
from app.config import settings
from app.workers.profiles import WorkerProfile, validate_worker_profile
from app.workers.runtime_contract import WorkerContract

RouteKind = Literal["MOCK", "PYTHON", "ANTHROPIC"]


class HarnessRouteError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class HarnessExecutionRoute:
    worker_id: str
    route_kind: RouteKind
    profile: WorkerProfile
    model_tier: str
    model_id: str | None
    max_output_tokens: int
    billing_currency: str | None


def resolve_worker_route(
    worker: WorkerContract,
    *,
    provider_mode: str | None = None,
    live_provider_enabled: bool | None = None,
) -> HarnessExecutionRoute:
    """Resolve a worker route with strict profile and live-provider gates."""
    mode = str(provider_mode or settings.WORKER_PROVIDER_MODE).strip().upper()
    live_enabled = (
        settings.WORKER_LIVE_PROVIDER_ENABLED
        if live_provider_enabled is None
        else bool(live_provider_enabled)
    )
    if not settings.WORKER_RUNTIME_ENABLED:
        raise HarnessRouteError("WORKER_RUNTIME_DISABLED", "Worker runtime is disabled.")
    try:
        profile = validate_worker_profile(worker.worker_id, worker.model_tier)
    except ValueError as exc:
        raise HarnessRouteError("WORKER_PROFILE_INVALID", str(exc)) from exc

    if mode == "MOCK":
        return HarnessExecutionRoute(
            worker.worker_id, "MOCK", profile, profile.model_tier,
            None, profile.max_output_tokens, None,
        )
    if mode != "ANTHROPIC":
        raise HarnessRouteError(
            "UNKNOWN_PROVIDER_MODE",
            "WORKER_PROVIDER_MODE must be MOCK or ANTHROPIC; legacy OmniRoute is disabled.",
        )

    if profile.execution_mode == "PYTHON":
        return HarnessExecutionRoute(
            worker.worker_id, "PYTHON", profile, "NONE", None, 0, None,
        )

    if not profile.ai_enabled_by_default:
        raise HarnessRouteError("WORKER_AI_NOT_ALLOWED", "This worker profile is not enabled for AI.")
    if not live_enabled:
        raise HarnessRouteError("LIVE_PROVIDER_DISABLED", "Paid Anthropic inference is disabled.")

    try:
        tier_config = get_tier_config(profile.model_tier)
    except ValueError as exc:
        raise HarnessRouteError("MODEL_TIER_UNKNOWN", "Worker profile has an unknown model tier.") from exc
    return HarnessExecutionRoute(
        worker.worker_id,
        "ANTHROPIC",
        profile,
        profile.model_tier,
        tier_config["model"],
        profile.max_output_tokens,
        "USD",
    )

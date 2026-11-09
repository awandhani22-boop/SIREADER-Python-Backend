"""Provider boundary for generic workforce tasks; never invokes trading execution."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from threading import Event
from typing import Callable, Protocol
from uuid import uuid4

from app.ai import omniroute_client
from app.ai.cost_guard import resolve_billing, UnknownModelPricingError, get_tier_config
from app.config import settings
from app.workers.runtime_contract import WorkerContract


class WorkerProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, outcome_unknown: bool = False, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.outcome_unknown = outcome_unknown
        self.retryable = retryable


@dataclass(frozen=True)
class WorkerExecutionRequest:
    task_id: str
    task_type: str
    title: str
    input_payload: dict[str, object]
    correlation_id: str
    attempt_request_id: str
    required_capability: str = "unknown"
    model_tier: str = "NONE"
    policy_version: str = "2026.11.10"
    sensitive_action: str = "ALLOW"
    requires_human_review: bool = False
    cancellation: Event | None = None
    on_provider_reserved: Callable[[str], None] | None = None


@dataclass(frozen=True)
class WorkerProviderResult:
    result_payload: dict[str, object]
    source: str
    provider_request_id: str | None
    billing_currency: str | None
    cost_amount: str | None
    usage: dict[str, int] | None
    requires_human_review: bool = False


class WorkerProvider(Protocol):
    def execute(self, worker: WorkerContract, request: WorkerExecutionRequest) -> WorkerProviderResult: ...


class MockWorkerProvider:
    """Pure deterministic provider. It never reads credentials or calls a network."""

    def execute(self, worker: WorkerContract, request: WorkerExecutionRequest) -> WorkerProviderResult:
        if request.cancellation is not None and request.cancellation.is_set():
            raise WorkerProviderError("CANCELLED_BEFORE_EXECUTION", "Task cancelled before mock execution.")
        encoded = json.dumps(
            {"task_type": request.task_type, "title": request.title, "input": request.input_payload},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        fingerprint = hashlib.sha256(encoded).hexdigest()
        return WorkerProviderResult(
            result_payload={
                "summary": f"SIMULATED task accepted by {worker.display_name}.",
                "input_fingerprint_sha256": fingerprint,
                "input_field_count": len(request.input_payload),
                "worker_id": worker.worker_id,
                "task_type": request.task_type,
            },
            source="SIMULATED",
            provider_request_id=None,
            billing_currency=None,
            cost_amount=None,
            usage=None,
            requires_human_review=request.requires_human_review,
        )


class OmniRouteWorkerProvider:
    """Server-only OmniRoute adapter that delegates every charge to the existing Cost Guard."""

    def execute(self, worker: WorkerContract, request: WorkerExecutionRequest) -> WorkerProviderResult:
        if request.cancellation is not None and request.cancellation.is_set():
            raise WorkerProviderError("CANCELLED_BEFORE_REQUEST", "Task cancelled before provider request.")
        if worker.model_tier == "NONE":
            raise WorkerProviderError("WORKER_HAS_NO_MODEL_TIER", "This worker is not configured for model inference.")
        if not settings.AI_API_KEY or not settings.AI_BASE_URL:
            raise WorkerProviderError("PROVIDER_NOT_CONFIGURED", "Backend AI provider configuration is incomplete.")
        selected_tier = request.model_tier if request.model_tier in {"FAST", "REASONING", "ESCALATION"} else worker.model_tier
        if selected_tier == "NONE":
            raise WorkerProviderError("MODEL_TIER_POLICY_REJECTED", "Workforce policy did not authorize a provider model tier.")
        tier_config = get_tier_config(selected_tier)
        try:
            billing = resolve_billing(tier_config["model"])
        except UnknownModelPricingError as exc:
            raise WorkerProviderError("UNKNOWN_MODEL_PRICING", "Configured worker model has no approved price.") from exc
        if billing["currency"] == "IDR":
            task_cap = settings.WORKER_MAX_TASK_COST_RP
            max_cost = {"max_cost_rp": task_cap, "max_cost_usd": None}
        else:
            task_cap = settings.WORKER_MAX_TASK_COST_USD
            max_cost = {"max_cost_rp": None, "max_cost_usd": task_cap}
        if task_cap <= 0:
            raise WorkerProviderError("TASK_COST_CAP_NOT_CONFIGURED", "A positive per-task budget cap is required before live inference.")
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a SIREADER Virtual Office workforce worker. Perform only the supplied non-execution work task. "
                    "Do not place, modify, or close trades; do not call tools; do not claim external actions. "
                    "Return a concise result grounded only in the supplied task input."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task_id": request.task_id,
                        "task_type": request.task_type,
                        "title": request.title,
                        "input": request.input_payload,
                    },
                    ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                ),
            },
        ]
        try:
            result = omniroute_client.call_ai(
                tier=selected_tier,
                messages=messages,
                request_id=request.attempt_request_id,
                on_reserved=request.on_provider_reserved,
                **max_cost,
            )
        except Exception as exc:  # provider errors are intentionally not retried here
            # CostGuard keeps the reservation UNKNOWN whenever the request may have reached the provider.
            from app.ai.cost_guard import BudgetExceededError
            if isinstance(exc, BudgetExceededError):
                raise WorkerProviderError("BUDGET_BLOCKED", "Cost Guard rejected the per-task or global reservation.") from exc
            if isinstance(exc, omniroute_client.AIRequestError) and exc.definitely_not_charged:
                raise WorkerProviderError(
                    "PROVIDER_STATE_PERSIST_FAILED",
                    "Provider request was not sent because its reserved state could not be persisted.",
                    retryable=True,
                ) from exc
            raise WorkerProviderError("PROVIDER_OUTCOME_UNKNOWN", "Provider call failed; reconcile its usage reservation before retrying.", outcome_unknown=True) from exc
        raw_usage = result.get("usage")
        usage: dict[str, int] | None = None
        if isinstance(raw_usage, dict):
            try:
                parsed_usage = {
                    key: raw_usage[key]
                    for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
                }
                if any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in parsed_usage.values()
                ):
                    raise ValueError("invalid token usage")
                usage = parsed_usage
            except (KeyError, TypeError, ValueError):
                usage = None
        if usage is None:
            # The provider may already have completed and charged this request.
            # Never silently report zero usage or send it through another model.
            raise WorkerProviderError(
                "PROVIDER_USAGE_UNAVAILABLE",
                "Provider completed without a valid token-usage record; reconcile before retrying.",
                outcome_unknown=True,
            )
        return WorkerProviderResult(
            result_payload={
                "content": result["content"],
                "tier": result["tier"],
                "model": result["model"],
            },
            source="LIVE",
            provider_request_id=result["request_id"],
            billing_currency=result["billing_currency"],
            cost_amount=str(result["cost"]),
            usage=usage,
            requires_human_review=request.requires_human_review,
        )


def create_worker_provider(worker: WorkerContract) -> WorkerProvider:
    """Resolve configured provider only after the independent runtime safety switch is enabled."""
    if not settings.WORKER_RUNTIME_ENABLED:
        raise WorkerProviderError("WORKER_RUNTIME_DISABLED", "Worker runtime is disabled by configuration.")
    mode = str(settings.WORKER_PROVIDER_MODE).strip().upper()
    if mode == "MOCK":
        return MockWorkerProvider()
    if mode == "OMNIROUTE":
        if not settings.WORKER_LIVE_PROVIDER_ENABLED:
            raise WorkerProviderError("LIVE_PROVIDER_DISABLED", "Live provider execution is disabled by configuration.")
        if worker.model_tier == "NONE":
            raise WorkerProviderError("WORKER_HAS_NO_MODEL_TIER", "This worker has no provider model tier.")
        return OmniRouteWorkerProvider()
    raise WorkerProviderError("UNKNOWN_PROVIDER_MODE", "WORKER_PROVIDER_MODE must be MOCK or OMNIROUTE.")

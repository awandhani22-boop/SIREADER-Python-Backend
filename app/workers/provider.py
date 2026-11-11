"""Central worker-provider boundary: deterministic Python profiles or Anthropic AI profiles."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from threading import Event
from typing import Callable, Protocol

from app.ai import provider_gateway
from app.ai.cost_guard import BudgetExceededError, resolve_billing, UnknownModelPricingError, get_tier_config
from app.config import settings
from app.workers.profiles import profile_for_worker, validate_worker_profile
from app.workers.harness import HarnessRouteError, resolve_worker_route
from app.workers.registry import CAPABILITY_CATALOG, eligible_workers
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
    requires_provider_request_tracking: bool

    def execute(self, worker: WorkerContract, request: WorkerExecutionRequest) -> WorkerProviderResult: ...


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise WorkerProviderError("PYTHON_TASK_PAYLOAD_INVALID", "Task payload must be bounded JSON.") from exc


def _python_result(worker: WorkerContract, request: WorkerExecutionRequest, payload: dict) -> WorkerProviderResult:
    return WorkerProviderResult(
        result_payload={
            **payload,
            "worker_id": worker.worker_id,
            "task_type": request.task_type,
            "execution_mode": "PYTHON",
        },
        source="PYTHON",
        provider_request_id=None,
        billing_currency=None,
        cost_amount=None,
        usage=None,
        requires_human_review=request.requires_human_review,
    )


def _path_value(root: dict, path: str):
    value: object = root
    for segment in path.split("."):
        if not isinstance(value, dict) or segment not in value:
            return False, None
        value = value[segment]
    return True, value


def _evaluate_rule(rule: dict, facts: dict) -> dict:
    path = rule.get("path")
    operator = rule.get("operator")
    if not isinstance(path, str) or not path.strip():
        raise WorkerProviderError("PYTHON_RULE_SCHEMA_INVALID", "Each rule needs a non-empty facts path.")
    exists, actual = _path_value(facts, path)
    expected = rule.get("expected")
    passed = False
    if operator == "exists":
        passed = exists is bool(expected if expected is not None else True)
    elif not exists:
        passed = False
    elif operator == "eq":
        passed = actual == expected
    elif operator == "ne":
        passed = actual != expected
    elif operator in {"gt", "gte", "lt", "lte"}:
        if isinstance(actual, bool) or not isinstance(actual, (int, float, Decimal)):
            raise WorkerProviderError("PYTHON_RULE_TYPE_INVALID", f"Numeric operator requires numeric fact at {path}.")
        if isinstance(expected, bool) or not isinstance(expected, (int, float, Decimal)):
            raise WorkerProviderError("PYTHON_RULE_TYPE_INVALID", f"Numeric operator requires numeric expected value at {path}.")
        passed = {
            "gt": actual > expected,
            "gte": actual >= expected,
            "lt": actual < expected,
            "lte": actual <= expected,
        }[operator]
    elif operator == "in":
        if not isinstance(expected, list):
            raise WorkerProviderError("PYTHON_RULE_SCHEMA_INVALID", "The 'in' operator requires an expected list.")
        passed = actual in expected
    elif operator == "contains":
        if not isinstance(actual, (str, list, tuple, set, dict)):
            raise WorkerProviderError("PYTHON_RULE_TYPE_INVALID", f"Contains requires a collection at {path}.")
        passed = expected in actual
    else:
        raise WorkerProviderError("PYTHON_RULE_OPERATOR_UNSUPPORTED", f"Unsupported rule operator: {operator!r}.")
    return {
        "rule_id": str(rule.get("id") or path),
        "path": path,
        "operator": operator,
        "passed": bool(passed),
        "actual": actual if exists and isinstance(actual, (str, int, float, bool, type(None), list, dict)) else None,
        "expected": expected,
    }


class MockWorkerProvider:
    """Pure deterministic mock. It never reads credentials or calls a network."""
    requires_provider_request_tracking = False

    def execute(self, worker: WorkerContract, request: WorkerExecutionRequest) -> WorkerProviderResult:
        if request.cancellation is not None and request.cancellation.is_set():
            raise WorkerProviderError("CANCELLED_BEFORE_EXECUTION", "Task cancelled before mock execution.")
        encoded = _canonical_json({
            "task_type": request.task_type, "title": request.title, "input": request.input_payload,
        }).encode("utf-8")
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


class PythonWorkerProvider:
    """Deterministic handlers. Unsupported operations fail rather than pretending success."""
    requires_provider_request_tracking = False

    def execute(self, worker: WorkerContract, request: WorkerExecutionRequest) -> WorkerProviderResult:
        if not settings.WORKER_RUNTIME_ENABLED or str(settings.WORKER_PROVIDER_MODE).upper() != "ANTHROPIC":
            raise WorkerProviderError("WORKER_RUNTIME_DISABLED", "Python worker execution requires the explicitly enabled ANTHROPIC hybrid runtime mode.")
        if request.cancellation is not None and request.cancellation.is_set():
            raise WorkerProviderError("CANCELLED_BEFORE_EXECUTION", "Task cancelled before Python execution.")
        profile = validate_worker_profile(worker.worker_id, worker.model_tier)
        if profile.execution_mode != "PYTHON":
            raise WorkerProviderError("WORKER_PROFILE_REQUIRES_AI", "This worker profile is not a deterministic Python worker.")
        payload = request.input_payload

        if worker.worker_id == "decision_engineer":
            return self._decision_engineer(worker, request, payload)
        if worker.worker_id == "market_monitor":
            return self._market_monitor(worker, request, payload)
        if worker.worker_id == "data_archivist":
            return self._data_archivist(worker, request, payload)
        if worker.worker_id == "supervisor":
            return self._supervisor(worker, request, payload)
        raise WorkerProviderError("PYTHON_HANDLER_NOT_IMPLEMENTED", "No Python handler is registered for this worker.")

    def _decision_engineer(self, worker, request, payload):
        if request.task_type not in {
            "RULE_VALIDATION", "DECISION_EVALUATION",
            "RISK_ASSESSMENT_SIMULATION", "DECISION_CONSISTENCY_CHECKS",
        }:
            raise WorkerProviderError("PYTHON_HANDLER_NOT_IMPLEMENTED", "Decision Engineer task type is not supported.")
        facts = payload.get("facts")
        rules = payload.get("checks", payload.get("rules"))
        if not isinstance(facts, dict) or not isinstance(rules, list) or not rules:
            raise WorkerProviderError(
                "PYTHON_RULE_SCHEMA_INVALID",
                "Decision Engineer requires a 'facts' object and a non-empty 'checks' list.",
            )
        results = [_evaluate_rule(rule, facts) for rule in rules if isinstance(rule, dict)]
        if len(results) != len(rules):
            raise WorkerProviderError("PYTHON_RULE_SCHEMA_INVALID", "Every check must be an object.")
        failed = [item for item in results if not item["passed"]]
        return _python_result(worker, request, {
            "decision": "PASS" if not failed else "REVIEW",
            "passed": not failed,
            "checks": results,
            "failed_check_count": len(failed),
            "order_sent": False,
            "note": "Deterministic policy evaluation only; no trading action was performed.",
        })

    def _market_monitor(self, worker, request, payload):
        if request.task_type not in {
            "MARKET_MONITORING_SIMULATION", "MARKET_CONDITION_DETECTION",
            "ANOMALY_DETECTION_SIMULATION", "MARKET_EVENT_REPORTING",
        }:
            raise WorkerProviderError("PYTHON_HANDLER_NOT_IMPLEMENTED", "Market Monitor task type is not supported.")
        snapshot = payload.get("market_data")
        if not isinstance(snapshot, dict):
            raise WorkerProviderError("MARKET_SNAPSHOT_INVALID", "Market Monitor only accepts a supplied market_data object.")
        bid, ask = snapshot.get("bid"), snapshot.get("ask")
        observed = snapshot.get("observed_at")
        symbol = snapshot.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise WorkerProviderError("MARKET_SNAPSHOT_INVALID", "A supplied symbol is required.")
        if (
            isinstance(bid, bool) or not isinstance(bid, (int, float))
            or isinstance(ask, bool) or not isinstance(ask, (int, float))
            or bid <= 0 or ask <= 0
        ):
            raise WorkerProviderError("MARKET_SNAPSHOT_INVALID", "Positive numeric bid and ask are required.")
        if not isinstance(observed, str):
            raise WorkerProviderError("MARKET_TIMESTAMP_MISSING", "observed_at with timezone is required.")
        try:
            observed_at = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        except ValueError as exc:
            raise WorkerProviderError("MARKET_TIMESTAMP_INVALID", "observed_at must be ISO-8601.") from exc
        if observed_at.tzinfo is None:
            raise WorkerProviderError("MARKET_TIMESTAMP_INVALID", "observed_at must include a timezone.")
        age_seconds = (datetime.now(timezone.utc) - observed_at.astimezone(timezone.utc)).total_seconds()
        if age_seconds < -5:
            freshness = "FUTURE_TIMESTAMP"
        elif age_seconds > int(settings.WORKER_MARKET_DATA_MAX_AGE_SECONDS):
            freshness = "STALE"
        else:
            freshness = "FRESH"
        crossed = ask < bid
        return _python_result(worker, request, {
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "spread": round(float(ask) - float(bid), 8),
            "observed_at": observed_at.isoformat(),
            "age_seconds": round(age_seconds, 3),
            "freshness": freshness,
            "anomaly": "CROSSED_BOOK" if crossed else None,
            "observation_source": str(snapshot.get("source") or "SUPPLIED_SNAPSHOT"),
            "note": "Only supplied snapshot was inspected; no live market connection or trade action occurred.",
            "requires_human_review": freshness != "FRESH" or crossed,
        })

    def _data_archivist(self, worker, request, payload):
        if request.task_type not in {
            "DATA_ARCHIVING", "ARTIFACT_INDEXING",
            "HISTORICAL_RETRIEVAL_SIMULATION", "DATA_ORGANIZATION",
        }:
            raise WorkerProviderError("PYTHON_HANDLER_NOT_IMPLEMENTED", "Data Archivist task type is not supported.")
        encoded = _canonical_json(payload).encode("utf-8")
        manifest = {
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "payload_bytes": len(encoded),
            "top_level_fields": sorted(str(key) for key in payload),
            "record_count": len(payload.get("records", [])) if isinstance(payload.get("records"), list) else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "storage_action": "MANIFEST_ONLY",
        }
        return _python_result(worker, request, {
            "manifest": manifest,
            "note": "Manifest metadata generated; no external archive or file write was performed.",
        })

    def _supervisor(self, worker, request, payload):
        if request.task_type not in {
            "INSTRUCTION_INTAKE", "ROUTING", "TASK_DECOMPOSITION",
            "WORKFLOW_SUPERVISION", "TASK_ASSIGNMENT_OVERSIGHT", "RESULT_REVIEW",
        }:
            raise WorkerProviderError("PYTHON_HANDLER_NOT_IMPLEMENTED", "Supervisor Python task type is not supported.")
        subtasks = payload.get("subtasks")
        if subtasks is not None:
            if not isinstance(subtasks, list) or not subtasks:
                raise WorkerProviderError("SUPERVISOR_PLAN_INVALID", "subtasks must be a non-empty list.")
            plan = []
            for index, item in enumerate(subtasks):
                if not isinstance(item, dict):
                    raise WorkerProviderError("SUPERVISOR_PLAN_INVALID", "Every subtask must be an object.")
                capability = item.get("required_capability")
                if capability not in CAPABILITY_CATALOG:
                    raise WorkerProviderError("SUPERVISOR_CAPABILITY_UNKNOWN", f"Unknown capability in subtask {index}.")
                candidates = eligible_workers(capability)
                plan.append({
                    "sequence": index + 1,
                    "task_type": item.get("task_type"),
                    "required_capability": capability,
                    "candidate_worker_ids": [candidate.worker_id for candidate in candidates],
                    "dispatch_performed": False,
                })
            return _python_result(worker, request, {
                "plan": plan,
                "dispatch_performed": False,
                "note": "Deterministic routing plan only; no child tasks were enqueued.",
            })
        capability = payload.get("required_capability")
        if capability in CAPABILITY_CATALOG:
            candidates = eligible_workers(capability)
            return _python_result(worker, request, {
                "required_capability": capability,
                "candidate_worker_ids": [candidate.worker_id for candidate in candidates],
                "dispatch_performed": False,
            })
        raise WorkerProviderError(
            "SUPERVISOR_PLAN_SCHEMA_UNSUPPORTED",
            "Provide required_capability or a pre-decomposed subtasks list. Free-form decomposition requires the separately gated Supervisor AI capability.",
        )


class AnthropicWorkerProvider:
    """Shared Anthropic gateway adapter. Exactly one billable request per task attempt."""
    requires_provider_request_tracking = True

    def execute(self, worker: WorkerContract, request: WorkerExecutionRequest) -> WorkerProviderResult:
        if not settings.WORKER_RUNTIME_ENABLED:
            raise WorkerProviderError("WORKER_RUNTIME_DISABLED", "Worker runtime is disabled.")
        if str(settings.WORKER_PROVIDER_MODE).upper() != "ANTHROPIC":
            raise WorkerProviderError("UNKNOWN_PROVIDER_MODE", "Anthropic calls require WORKER_PROVIDER_MODE=ANTHROPIC.")
        if not settings.WORKER_LIVE_PROVIDER_ENABLED:
            raise WorkerProviderError("LIVE_PROVIDER_DISABLED", "Paid Anthropic inference is disabled.")
        if str(settings.AI_PROVIDER or "").strip().lower() != "anthropic":
            raise WorkerProviderError(
                "ANTHROPIC_PROVIDER_REQUIRED",
                "AI_PROVIDER must be set to anthropic for the worker's official Anthropic route.",
            )
        if not settings.ANTHROPIC_API_KEY:
            raise WorkerProviderError("PROVIDER_NOT_CONFIGURED", "ANTHROPIC_API_KEY is not configured.")
        if request.cancellation is not None and request.cancellation.is_set():
            raise WorkerProviderError("CANCELLED_BEFORE_REQUEST", "Task cancelled before Anthropic request.")
        try:
            profile = validate_worker_profile(worker.worker_id, worker.model_tier)
        except ValueError as exc:
            raise WorkerProviderError("WORKER_PROFILE_INVALID", str(exc)) from exc
        if profile.execution_mode != "AI" or not profile.ai_enabled_by_default:
            raise WorkerProviderError("WORKER_AI_NOT_ALLOWED", "This worker profile is not authorized for AI inference.")
        if request.model_tier != profile.model_tier:
            raise WorkerProviderError(
                "MODEL_TIER_POLICY_REJECTED",
                "Requested tier must exactly match the worker profile's fixed model tier.",
            )
        tier_config = get_tier_config(profile.model_tier)
        try:
            billing = resolve_billing(tier_config["model"])
        except UnknownModelPricingError as exc:
            raise WorkerProviderError("UNKNOWN_MODEL_PRICING", "Configured Anthropic model has no approved price.") from exc
        if billing["currency"] != "USD":
            raise WorkerProviderError("ANTHROPIC_BILLING_REQUIRED", "AI worker profiles must use the official Anthropic USD price table.")
        task_cap = settings.WORKER_MAX_TASK_COST_USD
        if task_cap <= 0:
            raise WorkerProviderError(
                "TASK_COST_CAP_NOT_CONFIGURED",
                "A positive per-task USD cap is required before live Anthropic inference.",
            )
        messages = [
            {
                "role": "system",
                "content": (
                    f"You are the SIREADER {worker.display_name}. Perform only the assigned non-execution task. "
                    "Never place, modify, or close trades. Never claim external actions. "
                    "Use only the supplied input and return a concise, verifiable result."
                ),
            },
            {
                "role": "user",
                "content": _canonical_json({
                    "task_id": request.task_id,
                    "task_type": request.task_type,
                    "title": request.title,
                    "required_capability": request.required_capability,
                    "input": request.input_payload,
                }),
            },
        ]
        try:
            result = provider_gateway.call_ai(
                tier=profile.model_tier,
                messages=messages,
                max_output_tokens=profile.max_output_tokens,
                request_id=request.attempt_request_id,
                on_reserved=request.on_provider_reserved,
                max_cost_usd=task_cap,
            )
        except BudgetExceededError as exc:
            raise WorkerProviderError("BUDGET_BLOCKED", "Cost Guard rejected the per-task or global Anthropic budget.") from exc
        except Exception as exc:
            if isinstance(exc, provider_gateway.AIRequestError) and exc.definitely_not_charged:
                if getattr(exc, "code", None) == "TOKEN_COUNT_FAILED":
                    raise WorkerProviderError(
                        "TOKEN_COUNT_PREFLIGHT_FAILED",
                        "Anthropic token counting failed before paid inference; no model request was sent.",
                        retryable=True,
                    ) from exc
                if getattr(exc, "code", None) == "GLOBAL_PROVIDER_CONCURRENCY_LIMIT":
                    raise WorkerProviderError(
                        "GLOBAL_PROVIDER_CONCURRENCY_LIMIT",
                        "The single global provider slot is occupied; this request was not sent.",
                        retryable=True,
                    ) from exc
                raise WorkerProviderError(
                    "PROVIDER_STATE_PERSIST_FAILED",
                    "The Anthropic request was not sent because pre-request state could not be persisted.",
                    retryable=True,
                ) from exc
            raise WorkerProviderError(
                "PROVIDER_OUTCOME_UNKNOWN",
                "Anthropic request outcome is unknown; reconcile the reservation before any retry.",
                outcome_unknown=True,
            ) from exc

        raw_usage = result.get("usage")
        if not isinstance(raw_usage, dict):
            raise WorkerProviderError(
                "PROVIDER_USAGE_UNAVAILABLE",
                "Anthropic returned no valid usage record; reconciliation is required.",
                outcome_unknown=True,
            )
        usage = {
            key: raw_usage.get(key, 0)
            for key in (
                "input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens",
            )
        }
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in usage.values()):
            raise WorkerProviderError(
                "PROVIDER_USAGE_UNAVAILABLE",
                "Anthropic usage values are invalid; reconciliation is required.",
                outcome_unknown=True,
            )
        truncated = result.get("finish_reason") == "max_tokens"
        return WorkerProviderResult(
            result_payload={
                "content": result["content"],
                "tier": result["tier"],
                "model": result["model"],
                "finish_reason": result.get("finish_reason"),
                "output_truncated": truncated,
            },
            source="LIVE",
            provider_request_id=result["request_id"],
            billing_currency="USD",
            cost_amount=str(result["cost"]),
            usage=usage,
            requires_human_review=request.requires_human_review or truncated,
        )


# Backward-compatible class name for existing runtime/test imports; transport is Anthropic direct.
OmniRouteWorkerProvider = AnthropicWorkerProvider


def create_worker_provider(worker: WorkerContract) -> WorkerProvider:
    """Resolve the worker through the central control plane."""
    try:
        route = resolve_worker_route(worker)
    except HarnessRouteError as exc:
        raise WorkerProviderError(exc.code, str(exc)) from exc
    if route.route_kind == "MOCK":
        return MockWorkerProvider()
    if route.route_kind == "PYTHON":
        return PythonWorkerProvider()
    if route.route_kind == "ANTHROPIC":
        return AnthropicWorkerProvider()
    raise WorkerProviderError("HARNESS_ROUTE_INVALID", "Central harness returned an unsupported route.")

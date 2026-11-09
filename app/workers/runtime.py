"""Opt-in worker execution runtime. Not imported by app.main or the read-only API."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

from app.config import settings
from app.workers.policy import SensitiveTaskAction, evaluate_workforce_policy
from app.database.connection import SessionLocal
from app.workers.models import WorkerDefinitionRecord, WorkerCapabilityAssignmentRecord
from app.workers.provider import (
    OmniRouteWorkerProvider, WorkerExecutionRequest, WorkerProviderError,
    create_worker_provider,
)
from app.workers.repository import (
    claim_next_task, complete_task, fail_task, heartbeat_worker,
    mark_provider_requested, recover_expired_leases, renew_task_lease,
    start_task, expire_deadline_tasks, purge_expired_task_payloads,
    expire_stale_reviews, purge_expired_review_payloads,
)
from app.workers.runtime_contract import WorkerContract, WorkerHeartbeatContract, WorkerProcessState

logger = logging.getLogger(__name__)
MAINTENANCE_INTERVAL_SECONDS = 15
HEARTBEAT_INTERVAL_SECONDS = 15
_last_maintenance_at: datetime | None = None
MIN_LEASE_SECONDS = 5


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_worker_contract(db, worker_id: str) -> WorkerContract:
    row = db.get(WorkerDefinitionRecord, worker_id)
    if row is None:
        raise WorkerProviderError("WORKER_NOT_REGISTERED", "Worker is not registered.")
    capability_ids = db.query(WorkerCapabilityAssignmentRecord.capability_id).filter(
        WorkerCapabilityAssignmentRecord.worker_id == worker_id,
        WorkerCapabilityAssignmentRecord.enabled.is_(True),
    ).order_by(WorkerCapabilityAssignmentRecord.capability_id.asc()).all()
    return WorkerContract(
        worker_id=row.worker_id,
        display_name=row.display_name,
        role=row.role,
        department=row.department,
        capability_ids=[item[0] for item in capability_ids],
        enabled=row.enabled,
        max_concurrency=row.max_concurrency,
        model_tier=row.model_tier,
        schema_version=row.schema_version,
    )


def _heartbeat_contract(
    worker_id: str,
    instance_id: str,
    process_started_at: datetime,
    process_state: WorkerProcessState,
    *,
    now: datetime,
    active_task_count: int = 0,
    in_flight_provider_requests: int = 0,
) -> WorkerHeartbeatContract:
    return WorkerHeartbeatContract(
        worker_id=worker_id,
        instance_id=instance_id,
        process_state=process_state,
        process_started_at=process_started_at,
        heartbeat_at=now,
        active_task_count=active_task_count,
        in_flight_provider_requests=in_flight_provider_requests,
        observed_at=now,
    )


def _record_heartbeat(
    worker_id: str,
    instance_id: str,
    process_started_at: datetime,
    process_state: WorkerProcessState,
    *,
    active_task_count: int = 0,
    in_flight_provider_requests: int = 0,
) -> None:
    now = _utc_now()
    contract = _heartbeat_contract(
        worker_id, instance_id, process_started_at, process_state,
        now=now,
        active_task_count=active_task_count,
        in_flight_provider_requests=in_flight_provider_requests,
    )
    with SessionLocal() as db:
        with db.begin():
            heartbeat_worker(db, contract)


class _LeaseKeeper:
    """Independent heartbeat/lease thread while a synchronous provider call blocks."""
    def __init__(self, worker_id: str, instance_id: str, process_started_at: datetime,
                 task_id: str, lease_token: str, lease_seconds: int):
        self.worker_id = worker_id
        self.instance_id = instance_id
        self.process_started_at = process_started_at
        self.task_id = task_id
        self.lease_token = lease_token
        self.lease_seconds = lease_seconds
        self._provider_started = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"sireader-lease-{worker_id}", daemon=True)
        self.last_error: Exception | None = None

    def provider_started(self) -> None:
        self._provider_started.set()

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join(timeout=HEARTBEAT_INTERVAL_SECONDS + 2)

    def _run(self):
        while not self._stop.wait(HEARTBEAT_INTERVAL_SECONDS):
            try:
                now = _utc_now()
                with SessionLocal() as db:
                    with db.begin():
                        heartbeat_worker(db, _heartbeat_contract(
                            self.worker_id,
                            self.instance_id,
                            self.process_started_at,
                            WorkerProcessState.READY,
                            now=now,
                            active_task_count=1,
                            in_flight_provider_requests=1 if self._provider_started.is_set() else 0,
                        ))
                        renew_task_lease(
                            db, task_id=self.task_id, lease_token=self.lease_token,
                            lease_seconds=self.lease_seconds, now=now,
                        )
            except Exception as exc:
                self.last_error = exc
                logger.error(
                    "WORKER_LEASE_RENEWAL_FAILED worker_id=%s error_type=%s",
                    self.worker_id, type(exc).__name__,
                )
                return


def run_worker_once(
    worker_id: str,
    instance_id: str,
    process_started_at: datetime,
) -> bool:
    """Claim and execute at most one task. Returns False when the queue is idle."""
    if not settings.WORKER_RUNTIME_ENABLED:
        raise WorkerProviderError("WORKER_RUNTIME_DISABLED", "Set WORKER_RUNTIME_ENABLED=true to start workers.")

    global _last_maintenance_at
    now = _utc_now()
    _record_heartbeat(
        worker_id, instance_id, process_started_at, WorkerProcessState.READY,
    )
    run_maintenance = (
        _last_maintenance_at is None
        or (now - _last_maintenance_at).total_seconds() >= MAINTENANCE_INTERVAL_SECONDS
    )
    with SessionLocal() as db:
        with db.begin():
            if run_maintenance:
                recover_expired_leases(db, now=now)
                expire_deadline_tasks(db, now=now)
                purge_expired_task_payloads(db, now=now)
                expire_stale_reviews(db, now=now)
                purge_expired_review_payloads(db, now=now)
                _last_maintenance_at = now
            claimed = claim_next_task(
                db, worker_id=worker_id, instance_id=instance_id,
                lease_seconds=max(MIN_LEASE_SECONDS, int(settings.WORKER_TASK_LEASE_SECONDS)),
                heartbeat_timeout_seconds=int(settings.WORKER_HEARTBEAT_TIMEOUT_SECONDS),
                now=now,
            )
            if claimed is None:
                return False
            task_id = claimed[0].task_id
            attempt_id = claimed[1].attempt_id
            lease_token = claimed[1].lease_token
            task_type = claimed[0].task_type
            required_capability = claimed[0].required_capability
            title = claimed[0].title
            input_payload = dict(claimed[0].input_payload or {})
            correlation_id = claimed[0].correlation_id

    try:
        with SessionLocal() as db:
            worker = _load_worker_contract(db, worker_id)
        policy = evaluate_workforce_policy(
            worker=worker,
            task_type=task_type,
            required_capability=required_capability,
            title=title,
            input_payload=input_payload,
            provider_mode=settings.WORKER_PROVIDER_MODE,
        )
        if policy.action is SensitiveTaskAction.REJECT:
            with SessionLocal() as db:
                with db.begin():
                    fail_task(
                        db, task_id=task_id, lease_token=lease_token,
                        error_code=policy.reason_code,
                        retryable=False, outcome_unknown=False,
                    )
            logger.warning(
                "WORKER_GOVERNANCE_POLICY_REJECTED worker_id=%s task_id=%s policy_version=%s reason=%s",
                worker_id, task_id, policy.policy_version, policy.reason_code,
            )
            return True
        provider = create_worker_provider(worker)
    except Exception as exc:
        with SessionLocal() as db:
            with db.begin():
                fail_task(
                    db, task_id=task_id, lease_token=lease_token,
                    error_code=getattr(exc, "code", "PROVIDER_CONFIGURATION_FAILED"),
                    retryable=False, outcome_unknown=False,
                )
        logger.warning(
            "WORKER_PROVIDER_CONFIGURATION_REJECTED worker_id=%s task_id=%s error_type=%s",
            worker_id, task_id, type(exc).__name__,
        )
        return True

    task_started_at = _utc_now()
    with SessionLocal() as db:
        with db.begin():
            start_task(db, task_id=task_id, lease_token=lease_token, now=task_started_at)
            heartbeat_worker(db, _heartbeat_contract(
                worker_id,
                instance_id,
                process_started_at,
                WorkerProcessState.READY,
                now=task_started_at,
                active_task_count=1,
            ))

    lease_seconds = max(
        MIN_LEASE_SECONDS,
        int(settings.WORKER_TASK_LEASE_SECONDS),
        int(settings.WORKER_TASK_TIMEOUT_SECONDS) + 30,
    )
    if lease_seconds > 600:
        with SessionLocal() as db:
            with db.begin():
                fail_task(
                    db, task_id=task_id, lease_token=lease_token,
                    error_code="LEASE_CONFIGURATION_INVALID",
                    retryable=False, outcome_unknown=False,
                )
        return True

    def persist_provider_reservation(_provider_request_id: str) -> None:
        reserved_at = _utc_now()
        with SessionLocal() as db:
            with db.begin():
                mark_provider_requested(
                    db, task_id=task_id, lease_token=lease_token,
                    provider_request_id=_provider_request_id, now=reserved_at,
                )
                heartbeat_worker(db, _heartbeat_contract(
                    worker_id,
                    instance_id,
                    process_started_at,
                    WorkerProcessState.READY,
                    now=reserved_at,
                    active_task_count=1,
                    in_flight_provider_requests=1,
                ))

    request = WorkerExecutionRequest(
        task_id=task_id,
        task_type=task_type,
        required_capability=required_capability,
        title=policy.sanitized_title,
        input_payload=policy.sanitized_input,
        correlation_id=correlation_id,
        attempt_request_id=attempt_id,
        model_tier=policy.model_tier,
        policy_version=policy.policy_version,
        sensitive_action=policy.action.value,
        requires_human_review=policy.requires_human_review,
    )

    try:
        with _LeaseKeeper(
            worker_id, instance_id, process_started_at,
            task_id, lease_token, lease_seconds,
        ) as lease_keeper:
            if isinstance(provider, OmniRouteWorkerProvider):
                original_request = request

                def tracked_callback(provider_request_id: str) -> None:
                    persist_provider_reservation(provider_request_id)
                    lease_keeper.provider_started()

                request = WorkerExecutionRequest(
                    task_id=original_request.task_id,
                    task_type=original_request.task_type,
                    required_capability=original_request.required_capability,
                    title=original_request.title,
                    input_payload=original_request.input_payload,
                    correlation_id=original_request.correlation_id,
                    attempt_request_id=original_request.attempt_request_id,
                    model_tier=original_request.model_tier,
                    policy_version=original_request.policy_version,
                    sensitive_action=original_request.sensitive_action,
                    requires_human_review=original_request.requires_human_review,
                    on_provider_reserved=tracked_callback,
                )
            result = provider.execute(worker, request)
            if lease_keeper.last_error is not None:
                raise WorkerProviderError(
                    "LEASE_RENEWAL_UNCERTAIN",
                    "Lease renewal failed during execution; result requires reconciliation.",
                    outcome_unknown=isinstance(provider, OmniRouteWorkerProvider),
                )
        with SessionLocal() as db:
            with db.begin():
                complete_task(
                    db, task_id=task_id, lease_token=lease_token,
                    result_payload={**result.result_payload, "source": result.source},
                    billing_currency=result.billing_currency,
                    cost_amount=result.cost_amount,
                    force_human_review=result.requires_human_review,
                    review_trigger_reason="SENSITIVE_TASK_REVIEW" if result.requires_human_review else None,
                    policy_context={
                        "policy_version": policy.policy_version,
                        "sensitive_action": policy.action.value,
                        "model_tier": policy.model_tier,
                        "reason_code": policy.reason_code,
                        "redacted_fields": policy.redacted_fields,
                    },
                    now=_utc_now(),
                )
        return True
    except WorkerProviderError as exc:
        with SessionLocal() as db:
            with db.begin():
                fail_task(
                    db, task_id=task_id, lease_token=lease_token,
                    error_code=exc.code, retryable=exc.retryable,
                    outcome_unknown=exc.outcome_unknown, now=_utc_now(),
                )
        return True
    except Exception as exc:
        with SessionLocal() as db:
            with db.begin():
                fail_task(
                    db, task_id=task_id, lease_token=lease_token,
                    error_code="WORKER_UNEXPECTED_FAILURE",
                    retryable=False,
                    outcome_unknown=isinstance(provider, OmniRouteWorkerProvider),
                    now=_utc_now(),
                )
        logger.error(
            "WORKER_TASK_FAILED worker_id=%s task_id=%s error_type=%s",
            worker_id, task_id, type(exc).__name__,
        )
        return True
    finally:
        try:
            _record_heartbeat(
                worker_id, instance_id, process_started_at, WorkerProcessState.READY,
            )
        except Exception as exc:
            logger.error(
                "WORKER_FINAL_HEARTBEAT_FAILED worker_id=%s error_type=%s",
                worker_id, type(exc).__name__,
            )


def run_worker_forever(worker_id: str, *, poll_interval_seconds: float = 1.0) -> None:
    if not settings.WORKER_RUNTIME_ENABLED:
        raise WorkerProviderError("WORKER_RUNTIME_DISABLED", "Worker runtime is disabled by configuration.")
    if not 0.1 <= poll_interval_seconds <= 30:
        raise ValueError("poll_interval_seconds must be between 0.1 and 30 seconds")
    instance_id = uuid4().hex
    process_started_at = _utc_now()
    _record_heartbeat(worker_id, instance_id, process_started_at, WorkerProcessState.STARTING)
    logger.info("WORKER_PROCESS_STARTING worker_id=%s instance_id=%s", worker_id, instance_id)
    try:
        while True:
            worked = run_worker_once(worker_id, instance_id, process_started_at)
            if not worked:
                time.sleep(poll_interval_seconds)
    except KeyboardInterrupt:
        logger.info("WORKER_PROCESS_STOP_REQUESTED worker_id=%s", worker_id)
    finally:
        _record_heartbeat(worker_id, instance_id, process_started_at, WorkerProcessState.STOPPED)
        logger.info("WORKER_PROCESS_STOPPED worker_id=%s", worker_id)

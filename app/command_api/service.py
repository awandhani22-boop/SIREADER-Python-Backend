"""Atomic command execution with durable idempotency and audit evidence."""
from __future__ import annotations
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Callable
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.workers.models import WorkforceCommandAuditRecord

COMMAND_ACTOR_ID = "local_operator"

class CommandConflictError(RuntimeError):
    pass

def _fingerprint(command_type: str, request_body: dict) -> str:
    canonical = json.dumps(
        {"command_type": command_type, "request": request_body},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    return sha256(canonical).hexdigest()

def _existing_result(db: Session, *, idempotency_key: str, request_fingerprint: str) -> dict | None:
    row = db.scalar(select(WorkforceCommandAuditRecord).where(
        WorkforceCommandAuditRecord.actor_id == COMMAND_ACTOR_ID,
        WorkforceCommandAuditRecord.idempotency_key == idempotency_key,
    ))
    if row is None:
        return None
    if row.request_fingerprint != request_fingerprint:
        raise CommandConflictError("IDEMPOTENCY_CONFLICT")
    result = dict(row.outcome)
    result["replayed"] = True
    return result

def execute_idempotent_command(
    db: Session, *, command_type: str, idempotency_key: str,
    request_body: dict, executor: Callable[[str], tuple[str, dict, str | None]],
) -> dict:
    """Commit the mutation and append-only audit in one transaction."""
    if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 128:
        raise ValueError("Idempotency-Key must contain 8..128 characters.")
    if not command_type or len(command_type) > 40:
        raise ValueError("Invalid command type.")
    fingerprint = _fingerprint(command_type, request_body)
    replay = _existing_result(db, idempotency_key=idempotency_key, request_fingerprint=fingerprint)
    if replay is not None:
        return replay
    command_id = uuid4().hex
    command_correlation_id = uuid4().hex
    try:
        with db.begin_nested():
            target_id, outcome, domain_correlation_id = executor(command_correlation_id)
            correlation_id = domain_correlation_id or command_correlation_id
            created_at = datetime.now(timezone.utc)
            result = {
                "command_id": command_id, "actor_id": COMMAND_ACTOR_ID,
                "command_type": command_type, "target_id": target_id,
                "correlation_id": correlation_id, "outcome": outcome,
                "created_at": created_at.isoformat(), "replayed": False,
            }
            db.add(WorkforceCommandAuditRecord(
                command_id=command_id, actor_id=COMMAND_ACTOR_ID,
                command_type=command_type, target_id=target_id,
                idempotency_key=idempotency_key, request_fingerprint=fingerprint,
                correlation_id=correlation_id, outcome=result, created_at=created_at,
            ))
            db.flush()
            return result
    except IntegrityError:
        replay = _existing_result(db, idempotency_key=idempotency_key, request_fingerprint=fingerprint)
        if replay is None:
            raise
        return replay

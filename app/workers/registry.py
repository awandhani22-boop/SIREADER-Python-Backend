"""Deterministic worker/capability registry matching the existing Virtual Office roster."""
from __future__ import annotations

from types import MappingProxyType

from app.workers.runtime_contract import CapabilityContract, WorkerContract


CAPABILITY_CATALOG: dict[str, CapabilityContract] = {
    "instruction_intake": CapabilityContract(capability_id="instruction_intake", description="Normalize operator instructions into bounded workforce requests.", task_types=["INSTRUCTION_INTAKE", "ROUTING"]),
    "task_decomposition": CapabilityContract(capability_id="task_decomposition", description="Decompose a non-trading work request into ordered subtasks.", task_types=["TASK_DECOMPOSITION", "ROUTING"]),
    "workflow_supervision": CapabilityContract(capability_id="workflow_supervision", description="Observe progress and checkpoint multi-stage work.", task_types=["WORKFLOW_SUPERVISION", "ROUTING"]),
    "task_assignment_oversight": CapabilityContract(capability_id="task_assignment_oversight", description="Check task-to-worker assignment suitability.", task_types=["TASK_ASSIGNMENT_OVERSIGHT", "ROUTING"]),
    "result_review": CapabilityContract(capability_id="result_review", description="Review completeness and quality of a non-trading work result.", task_types=["RESULT_REVIEW", "REVIEW"]),
    "quick_research": CapabilityContract(capability_id="quick_research", description="Perform bounded fast research over supplied content.", task_types=["QUICK_RESEARCH", "RESEARCH"]),
    "information_extraction": CapabilityContract(capability_id="information_extraction", description="Extract structured facts from supplied content.", task_types=["INFORMATION_EXTRACTION", "CLASSIFICATION"]),
    "summarization": CapabilityContract(capability_id="summarization", description="Summarize supplied non-sensitive work content.", task_types=["SUMMARIZATION", "CLASSIFICATION"]),
    "lightweight_classification": CapabilityContract(capability_id="lightweight_classification", description="Classify work items with a fixed schema.", task_types=["LIGHTWEIGHT_CLASSIFICATION", "CLASSIFICATION"]),
    "data_analysis": CapabilityContract(capability_id="data_analysis", description="Analyze bounded structured datasets.", task_types=["DATA_ANALYSIS", "REASONING"]),
    "comparison": CapabilityContract(capability_id="comparison", description="Compare supplied alternatives against explicit criteria.", task_types=["COMPARISON", "REASONING"]),
    "evaluation": CapabilityContract(capability_id="evaluation", description="Evaluate supplied artifacts against a checklist.", task_types=["EVALUATION", "REASONING"]),
    "structured_reasoning": CapabilityContract(capability_id="structured_reasoning", description="Perform multi-step reasoning with cited input evidence.", task_types=["STRUCTURED_REASONING", "REASONING"]),
    "complex_reasoning": CapabilityContract(capability_id="complex_reasoning", description="Handle explicitly escalated complex non-trading analysis.", task_types=["COMPLEX_REASONING", "ESCALATION"]),
    "strategic_analysis": CapabilityContract(capability_id="strategic_analysis", description="Produce strategy alternatives for an approved work objective.", task_types=["STRATEGIC_ANALYSIS", "ESCALATION"]),
    "deep_review": CapabilityContract(capability_id="deep_review", description="Perform independent deep review of a supplied artifact.", task_types=["DEEP_REVIEW", "ESCALATION"]),
    "decision_evaluation": CapabilityContract(capability_id="decision_evaluation", description="Evaluate non-execution decisions against supplied policy.", task_types=["DECISION_EVALUATION", "REVIEW"]),
    "rule_validation": CapabilityContract(capability_id="rule_validation", description="Validate an artifact against explicit deterministic rules.", task_types=["RULE_VALIDATION", "DECISION_CHECK"]),
    "risk_assessment_simulation": CapabilityContract(capability_id="risk_assessment_simulation", description="Evaluate hypothetical risk scenarios only; never place orders.", task_types=["RISK_ASSESSMENT_SIMULATION", "DECISION_CHECK"]),
    "decision_consistency_checks": CapabilityContract(capability_id="decision_consistency_checks", description="Check consistency of documented decisions without executing them.", task_types=["DECISION_CONSISTENCY_CHECKS", "DECISION_CHECK"]),
    "market_monitoring_simulation": CapabilityContract(capability_id="market_monitoring_simulation", description="Inspect simulated or supplied market observations only.", task_types=["MARKET_MONITORING_SIMULATION", "MARKET_WATCH"]),
    "market_condition_detection": CapabilityContract(capability_id="market_condition_detection", description="Detect patterns in supplied market observations without trading.", task_types=["MARKET_CONDITION_DETECTION", "MARKET_WATCH"]),
    "anomaly_detection_simulation": CapabilityContract(capability_id="anomaly_detection_simulation", description="Identify anomalies in simulated/supplied market data.", task_types=["ANOMALY_DETECTION_SIMULATION", "MARKET_WATCH"]),
    "market_event_reporting": CapabilityContract(capability_id="market_event_reporting", description="Report observed market events without execution authority.", task_types=["MARKET_EVENT_REPORTING", "MARKET_WATCH"]),
    "data_archiving": CapabilityContract(capability_id="data_archiving", description="Archive authorized work artifacts with provenance.", task_types=["DATA_ARCHIVING", "ARCHIVAL"]),
    "artifact_indexing": CapabilityContract(capability_id="artifact_indexing", description="Index authorized artifact metadata.", task_types=["ARTIFACT_INDEXING", "ARCHIVAL"]),
    "historical_retrieval_simulation": CapabilityContract(capability_id="historical_retrieval_simulation", description="Retrieve historical test/demo artifacts only.", task_types=["HISTORICAL_RETRIEVAL_SIMULATION", "ARCHIVAL"]),
    "data_organization": CapabilityContract(capability_id="data_organization", description="Organize approved data artifacts and metadata.", task_types=["DATA_ORGANIZATION", "ARCHIVAL"]),
}


_WORKER_SPECS = {
    "supervisor": ("Supervisor AI", "Central Router & Task Orchestrator", "Command", ("instruction_intake", "task_decomposition", "workflow_supervision", "task_assignment_oversight", "result_review"), 2, "NONE"),
    "haiku": ("Haiku Worker", "Fast Research & Intake Specialist", "Research", ("quick_research", "information_extraction", "summarization", "lightweight_classification"), 2, "FAST"),
    "sonnet": ("Sonnet Worker", "Deep Quantitative & Structural Analyst", "Analysis", ("data_analysis", "comparison", "evaluation", "structured_reasoning"), 1, "REASONING"),
    "opus": ("Opus Worker", "High-Complexity Escalation Architect", "Strategy", ("complex_reasoning", "strategic_analysis", "deep_review", "decision_evaluation"), 1, "ESCALATION"),
    "decision_engineer": ("Decision Engineer", "Risk Guardrail & Rule Validator", "Trading Operations", ("rule_validation", "decision_evaluation", "risk_assessment_simulation", "decision_consistency_checks"), 1, "NONE"),
    "market_monitor": ("Market Monitor", "Market Feed & Volatility Observer", "Market Monitoring", ("market_monitoring_simulation", "market_condition_detection", "anomaly_detection_simulation", "market_event_reporting"), 1, "NONE"),
    "data_archivist": ("Data Archivist", "Audit Ledger & Historical State Curator", "Archive", ("data_archiving", "artifact_indexing", "historical_retrieval_simulation", "data_organization"), 1, "NONE"),
}


def build_default_registry() -> dict[str, WorkerContract]:
    """Return fresh validated definitions; no worker is presumed process-online."""
    workers = {}
    for worker_id, spec in _WORKER_SPECS.items():
        display_name, role, department, capability_ids, concurrency, tier = spec
        workers[worker_id] = WorkerContract(
            worker_id=worker_id,
            display_name=display_name,
            role=role,
            department=department,
            capability_ids=list(capability_ids),
            enabled=True,
            max_concurrency=concurrency,
            model_tier=tier,
        )
    validate_registry(workers)
    return workers


def validate_registry(workers: dict[str, WorkerContract]) -> None:
    if not workers:
        raise ValueError("worker registry cannot be empty")
    for worker_id, worker in workers.items():
        if worker_id != worker.worker_id:
            raise ValueError("registry key must match worker_id")
        unknown = set(worker.capability_ids) - set(CAPABILITY_CATALOG)
        if unknown:
            raise ValueError(f"worker {worker_id} has unknown capabilities: {sorted(unknown)}")


def eligible_workers(capability_id: str, registry: dict[str, WorkerContract] | None = None) -> tuple[WorkerContract, ...]:
    """Return enabled workers in canonical roster order for one known capability."""
    if capability_id not in CAPABILITY_CATALOG:
        raise ValueError(f"unknown capability: {capability_id}")
    workers = registry or build_default_registry()
    return tuple(
        worker for worker in workers.values()
        if worker.enabled and capability_id in worker.capability_ids
    )


def get_default_registry_snapshot() -> tuple[WorkerContract, ...]:
    return tuple(build_default_registry().values())

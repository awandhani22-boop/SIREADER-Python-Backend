# Phase 4.2 — Provider Routing and Cost Guard

**Status:** implemented with a mock-first provider boundary; live execution remains disabled by default.

## Provider policy

Generic workforce jobs use a dedicated provider interface in app/workers/provider.py. The deterministic mock provider is local and labels every result SIMULATED. The server-side OmniRoute adapter is only selected when WORKER_RUNTIME_ENABLED=true, WORKER_PROVIDER_MODE=OMNIROUTE, and WORKER_LIVE_PROVIDER_ENABLED=true. The model tier comes from the backend worker registry; there is no client-supplied model ID and no browser credential.

## Cost and identity

OmniRoute calls are routed through app/ai/omniroute_client.call_ai and app/ai/cost_guard. The provider request ID is the persisted task-attempt ID so the reservation/finalization record can be reconciled. Per-task caps are enforced inside the reservation function before the provider request is sent. Currency is derived from the configured model prefix and cannot be mixed.

WORKER_MAX_TASK_COST_RP and WORKER_MAX_TASK_COST_USD default to zero. A live provider request therefore remains blocked until the owner intentionally configures a positive cap for the correct currency. Existing daily and rolling-period budget caps remain in force as additional checks. An unknown provider outcome retains the existing UNKNOWN reservation; the task must not be automatically retried.

## Timeouts, errors and cancellation

The adapter uses the existing OmniRoute client timeout and no hidden retry/fallback. Configuration and budget rejections are known pre-request failures. Network/HTTP/invalid-usage failures after send are treated as uncertain and require usage reconciliation. Cancellation is honored before provider dispatch; once a synchronous request may have left the process, the task is not retried automatically and the outcome is treated as uncertain. A provider SDK with cancellable request handles may be added only behind the same interface.

## Security and scope

All provider keys/base URLs remain in backend settings. No provider credential is returned to a worker DTO or BFF. Prompt policy explicitly forbids tools and external execution. This provider is not used for trading decisions and has no MT5 capability. Existing Telegram trading analysis keeps its current independent pipeline.

## Verification

test_worker_provider.py covers deterministic mock behavior, disabled-by-default gates, positive task-cap requirement, stable attempt request ID, IDR cap propagation, cancellation before dispatch, and worker model-tier restrictions. test_cost_guard.py additions cover cap enforcement and currency mismatch. All tests are mock-only; no paid request is made.
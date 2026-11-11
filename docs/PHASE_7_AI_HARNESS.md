# SIREADER Central AI Harness — Phase 7

**Status:** implementation and offline verification complete; live activation remains disabled
**Date:** 2026-11-10
**Scope:** Python backend only. No trading execution, Telegram listener activation, long-running worker process, paid Anthropic request, application-database migration, or Supabase LIVE change. A single-cycle Haiku MOCK acceptance and its isolated test-only migration are documented in Section 10.

## 1. Target architecture

SIREADER uses one central control plane with worker-specific profiles and one shared Anthropic gateway.

- **Worker registry and profiles** define identity, deterministic/AI execution mode, capability ceiling, and output token ceiling.
- **Policy engine** validates capability/task compatibility, redacts personal data, rejects prohibited execution requests, and fixes the worker's model tier before dispatch.
- **Python-first handlers** perform bounded deterministic work without a model call where supported.
- **AI Router** classifies trading-message bundles, skips only explicitly pure commentary, and routes ambiguous signal extraction through FAST → REASONING → ESCALATION as required.
- **Context preparation** keeps source text, reply/edit history, and trade-episode metadata available to the trading analyzer. Unknown, replied, edited, episode-linked, or trade-relevant content fails open to model analysis.
- **Shared Anthropic gateway** is the only billable inference boundary used by the trading AI pipeline and AI worker provider.
- **Cost Guard** reserves budget before paid inference, finalizes from Anthropic usage, separates USD and legacy IDR records, and preserves UNKNOWN outcomes instead of retrying a request that may have been charged.
- **Output validation** checks structured JSON/evidence and routes truncated output to MANUAL_REVIEW.
- **Observability** reports model, request, token/cache usage, currency, cost, analysis, worker, and task association.

A worker identity is not a permanently running Claude session. The three AI workers are capability profiles whose inference requests are dispatched only when a task is authorized.

## 2. Canonical seven-worker roster

| Worker | Execution mode | Model tier | Output ceiling |
|---|---|---|---:|
| Supervisor | Deterministic Python | NONE | — |
| Haiku Worker | Anthropic AI | FAST | 700 |
| Sonnet Worker | Anthropic AI | REASONING | 1,400 |
| Opus Worker | Anthropic AI | ESCALATION | 1,800 |
| Decision Engineer | Deterministic Python | NONE | — |
| Market Monitor | Deterministic Python | NONE | — |
| Data Archivist | Deterministic Python | NONE | — |

The agreed output ceilings are maximums, not targets. The gateway rejects a request that asks a tier for more than its configured ceiling. The Supervisor profile remains deterministic and does not receive free-form AI decomposition by default. Its bounded task schema produces a routing plan only; dispatch is a separate task-runtime operation.

The Python handlers are intentionally bounded:
- Decision Engineer evaluates explicitly supplied facts and rules; it never sends an order.
- Market Monitor inspects only supplied snapshots; it has no live market connection or trading authority.
- Data Archivist creates deterministic fingerprints/manifests; external archival writes are not implied.
- Supervisor validates capabilities and prepares a routing plan; the handler does not enqueue child tasks by itself.

## 3. Shared Anthropic request lifecycle

Every model invocation follows this order:

1. Validate selected tier, model ID, messages, endpoint, and output ceiling.
2. Build the official Anthropic Messages payload, placing system instructions in the top-level `system` field.
3. Call the official free `POST /v1/messages/count_tokens` endpoint with the same model/system/messages. Its response supplies the model-specific `input_tokens` count.
4. Reserve the estimated maximum request cost in Cost Guard using that token count and the configured maximum output tokens.
5. Persist the request correlation callback when called by a worker.
6. Send one billable `POST /v1/messages` request.
7. Normalize Anthropic `usage` (input, output, cache-read, cache-creation by TTL, and thinking metadata).
8. Replace the reservation with the calculated actual cost and return usage/cost metadata.

The count endpoint counts input content without creating a message. It is a preflight estimate; the completed Messages response's `usage` is the authoritative token usage for the request. Cached token categories are kept separate. Thinking tokens are metadata already included in `output_tokens` and must not be added a second time.

If token preflight fails, no paid Messages request has been dispatched; the gateway marks it as definitely not charged. If transport fails after dispatch may have occurred, the request is marked UNKNOWN and is not retried automatically. The operator must reconcile it before replay.

Production endpoint validation accepts only `https://api.anthropic.com`. A non-official HTTPS host is rejected so an Anthropic key cannot accidentally be forwarded to a proxy. A loopback HTTP exception exists only when the explicit test flag `AI_ALLOW_INSECURE_TEST_ENDPOINT` is enabled.

## 4. Model price table

The standard API price table in `app/config.py` is stored in USD per million tokens (MTok):

| Model | Input | Output | Cache write 5m | Cache write 1h | Cache read |
|---|---:|---:|---:|---:|---:|
| Haiku 5.5, prompt up to 100k | $0.10 | $0.50 | $0.125 | $0.20 | $0.01 |
| Haiku 5.5, prompt over 100k | $0.50 | $2.50 | $0.625 | $1.00 | $0.05 |
| Sonnet 5.5 | $2.00 | $10.00 | $2.50 | $4.00 | $0.10 |
| Opus 5.5 | $4.00 | $20.00 | $5.00 | $8.00 | $0.20 |

These are the standard global API rates used for local cost accounting. Any provider-side credits, tax, contract pricing, workspace adjustments, or invoice reconciliation must be treated separately. The Cost Guard calculates cost from Anthropic's reported usage and this local official price table; it is not a direct read of the Anthropic invoice/console balance.

Official references:
- https://www.anthropic.com/claude-haiku-5-5
- https://www.anthropic.com/claude-sonnet-5-5
- https://www.anthropic.com/claude-opus-5-5
- https://platform.claude.com/docs/en/api/messages/count_tokens

## 5. Budget and usage reporting

- Monthly Anthropic hard budget remains `AI_ANTHROPIC_MONTHLY_BUDGET_USD=5.00`.
- Tier-level actual and reserved usage remains visible in USD.
- A request reservation uses the exact `count_tokens` input count and configured output ceiling, priced conservatively at the highest applicable input/cache rate with a 50% safety factor.
- Actual cost is recalculated from the returned usage, including cache TTL details.
- Worker reports correlate `AIUsageLog.request_id` with `WorkTaskAttemptRecord.provider_request_id`. This produces cost and token totals per worker and task without adding database columns or requiring a migration.
- Non-worker pipeline calls (such as Telegram trading analysis) remain grouped under a null worker/task association and are not silently assigned to a Virtual Office worker.
- IDR/TokenMurah historical rows remain supported separately; the current configured direct Anthropic IDs are billed against the USD table.

## 6. Python-first Telegram prefilter

The existing aggregator labels message roles. The router now uses a conservative prefilter before calling Haiku:

- It skips only bundles where every message is explicitly classified as pure COMMENTARY, contains non-empty text, and has no reply linkage, edit history, trade episode, or other role.
- Missing/unknown labels, replies, edits, episode metadata, signals, entry conditions, SL/TP, Skip instructions, position updates, and ambiguous content fail open to AI.
- Skipped commentary remains stored in Telegram history; the filter does not delete messages.
- Model truncation is never treated as authoritative. It returns MANUAL_REVIEW.

This is deliberately conservative: reduce clearly unnecessary calls without throwing away a possibly trade-relevant mentor update.

## 7. Operational switches — remain OFF

The local defaults remain fail-closed:

```ini
AI_PROVIDER=anthropic
AI_BASE_URL=https://api.anthropic.com
AI_PIPELINE_ENABLED=False
WORKER_RUNTIME_ENABLED=False
WORKER_PROVIDER_MODE=MOCK
WORKER_LIVE_PROVIDER_ENABLED=False
WORKER_MAX_TASK_COST_USD=0
```

The backend configuration reads `AI_BASE_URL` and `ANTHROPIC_API_KEY`. A differently named variable such as `ANTHROPIC_BASE_URL` is not consumed by this settings model. Do not change the existing `.env` or point production traffic to a proxy; production gateway validation requires the official Anthropic hostname.

At the time of offline verification no Anthropic API key was present in the backend configuration. Therefore no live model request was attempted. Do not enable a worker until a separate operator decision provisions the key, approves a positive per-task cap and budget, chooses a synthetic canary, and authorizes the process.

## 8. Verification

Offline focused suite executed with synthetic unit-test environment variables and no real database URL:

- Provider gateway, token preflight, cost guard and legacy billing tests.
- Router filter and truncated output tests.
- Worker provider, runtime guard, and mock-flow tests.
- Seven worker profiles and policy tests.
- Per-worker/task billing report test.

Latest focused result before final verification: **195 passed, 17 skipped, 0 failed**. The skipped tests require the isolated PostgreSQL integration fixture.

The direct full-suite invocation cannot safely run from this checkout without environment setup: `test_connection.py` exits during collection when the owner's real `.env` database URL is absent, and the full suite with an intentionally unused loopback database URL stalled in a test. Do not use the populated application database as a test substitute. Use a fresh disposable PostgreSQL fixture for the database integration set.

Additional verification with the project's Python 3.12 environment at `C:\SIREADER\.venv`:
- `python -m compileall -q app tests/workers` ? passed.
- `python -m pip check` ? passed; no broken requirements.
- `git diff --check` ? passed.
The unrelated global Python installation has a FastAPI/Starlette mismatch, but it was not used for final project verification and was not modified.

No worker was started, no paid Anthropic request was made, and no migration or data mutation was performed on the owner application database or Supabase LIVE. The later final verification did apply schema migrations and synthetic fixtures only inside the isolated temporary PostgreSQL test cluster. No Git commit/push was made.


## 9. Final local verification — 2026-11-10

The previous pre-final verification counts above are historical. They are superseded by this isolated re-run:

| Test group | Result |
|---|---:|
| API, PostgreSQL privilege fixture, migrations, workforce command/runtime, review and governance | **229 passed, 0 failed, 0 skipped** |
| Central AI Harness, provider gateway, token preflight, USD/IDR cost guard and all worker unit tests | **212 passed, 0 failed** |
| Root backend regression files containing actual pytest functions/classes | **370 passed, 0 failed** |

The three groups are run separately and overlap; do not add the counts to claim a unique total. All tests used the project Python 3.12 environment. Database integration tests ran against a newly initialized PostgreSQL 16.6 cluster on loopback port 55432, database `sireader_test`, under the operating-system temporary directory. All database environment variables were directed to that cluster. It was not the application's local database and was not Supabase.

Additional checks:
- `python -m compileall -q app tests/workers tests/api app/command_api` — passed.
- `python -m pip check` — passed; no broken requirements.
- `git diff --check` — passed. Git emitted only its normal LF-to-CRLF working-copy notices.

Root test discovery must not indiscriminately collect every file named `test_*.py`: some are owner-operated smoke scripts rather than pytest tests. In particular, files with top-level database calls (and the MT5 read-only diagnostic) are not automated tests. Only the 40 root files with real pytest test functions/classes were included in the 370-test run. `test_connection.py`, `test_context_reader.py`, `test_edit_history.py`, `test_reply_context.py`, `test_save_message.py`, `test_media_integrity.py`, `test_mt5_readonly.py`, and the print-only `test_config.py` were not executed.

One non-blocking existing warning remains: Starlette's `TestClient` integration with `httpx` is deprecated and suggests using `httpx2`. No dependency migration was made as part of this phase.

The temporary PostgreSQL cluster is stopped and removed after test completion. The owner application database and local `.env` are not modified. The live AI canary remains pending a separately approved Anthropic credential, per-task budget cap, synthetic canary task, and explicit operator authorization.



## 10. Phase 7.2 — foreground MOCK worker runtime acceptance (2026-11-10)

A real single-cycle invocation of `runtime.run_worker_once("haiku", ...)` was executed against a newly created, isolated PostgreSQL 16.6 cluster on loopback port 55432 (`sireader_test`). The harness used the project Python 3.12 environment and temporarily bound SQLAlchemy to this test-only loopback engine with SSL disabled because the temporary PostgreSQL server did not offer SSL. No application connection configuration or `.env` file was changed.

Both deterministic QUICK_RESEARCH review-policy branches passed:

| Case | Task result | Review state | Payload release |
|---|---|---|---|
| Deterministic sample bucket (20% policy) | COMPLETED / SIMULATED | PENDING | Quarantined; `task.result_payload` remains NULL |
| Outside sample bucket | COMPLETED / SIMULATED | No review row | Normal result payload released |

For both cases, the task was assigned to `haiku`, its sole attempt completed, provider request ID was NULL, billing currency/cost were NULL, and the worker heartbeat was explicitly returned to STOPPED with zero active tasks and zero in-flight provider requests. The sampled case emitted `HUMAN_REVIEW_QUEUED`; the unsampled case did not.

The first unsampled acceptance attempt exposed a DB-trigger mismatch: the review release trigger required an approved review row even when the authoritative policy intentionally created no review. The additive migration `migrations/2026_11_10_workforce_review_release_boundary_v2.sql` now permits a trusted worker writer to release a result only when no review record exists; a present PENDING/expired/purged review still blocks release. The command API database identity `sireader_workforce_cmd` remains unable to use the no-review exception and still must release only an approved/edited quarantined output. The matching rollback is `migrations/2026_11_10_workforce_review_release_boundary_v2_rollback.sql`.

The new migration was applied only to the disposable `sireader_test` cluster. Direct database probes confirmed (1) command-role publication without an approved review is blocked and (2) a PENDING review payload cannot be released. The migration is database-name guarded and must not be applied to Supabase LIVE as part of this acceptance.

No long-running worker process was left running. No paid Anthropic request, Telegram action, MT5/HFM action, application DB write, Supabase LIVE operation, Git commit, or push occurred. Temporary database records are synthetic and are removed with the isolated cluster during cleanup.


## 11. Local OmniRoute gateway preparation — 2026-11-10

The unified workspace now selects OmniRoute as the transport for the existing Anthropic Messages protocol. This supersedes the direct-provider-only endpoint note above for the current local workspace; the historical direct Anthropic configuration remains available by selecting `AI_PROVIDER=anthropic` and using the official endpoint.

Current backend `.env` configuration:

```ini
AI_PROVIDER=omniroute
AI_BASE_URL=http://127.0.0.1:20128
AI_API_KEY=
ANTHROPIC_API_KEY=
AI_MODEL_FAST=anthropic/claude-haiku-5-5
AI_MODEL_REASONING=anthropic/claude-sonnet-5-5
AI_MODEL_ESCALATION=anthropic/claude-opus-5-5
```

`AI_API_KEY` is intentionally blank pending the operator's local entry of the OmniRoute gateway API key named `SIREADER_CLAUDE`. Do not copy the upstream Anthropic provider credential into the backend. In OmniRoute mode the gateway selects only `AI_API_KEY`; it will not fall back to `ANTHROPIC_API_KEY`. OmniRoute's Anthropic Messages route accepts the native `x-api-key` header and exposes `POST /v1/messages/count_tokens` and `POST /v1/messages`.

The backend base URL is the gateway root (without `/v1`); the Messages adapter constructs `/v1/messages` and `/v1/messages/count_tokens`. In `AI_PROVIDER=omniroute` mode, the endpoint allowlist accepts only HTTP loopback (`127.0.0.1`, `localhost`, or `::1`) on port `20128`, without URL credentials, query, or fragment. It does not allow arbitrary LAN hosts, remote HTTPS proxies, or other ports to receive the gateway key. Direct `AI_PROVIDER=anthropic` mode continues to require `https://api.anthropic.com`.

A read-only OmniRoute metadata check found one active Anthropic upstream connection and one active `SIREADER_CLAUDE` gateway key. Its allowlist is exactly the three model IDs shown above; usage logs were empty at the time of the check. The checker did not read or print any key material. The local OmniRoute listener was reachable on loopback port `20128`.

The model IDs resolve through Cost Guard to USD billing and the expected Haiku/Sonnet/Opus pricing families. The configured monthly USD budget remains 5.00. OmniRoute/provider-side invoice or credit adjustments are not automatically reflected by the local Anthropic standard-rate estimator; reconcile actual provider billing separately.

The following switches remain fail-closed in the unified backend `.env`: `AI_PIPELINE_ENABLED=False`, `WORKER_RUNTIME_ENABLED=False`, `WORKER_PROVIDER_MODE=MOCK`, `WORKER_LIVE_PROVIDER_ENABLED=False`, `WORKER_MAX_TASK_COST_USD=0`, `TRADING_MODE=DRY_RUN`, and `AUTO_EXECUTION_ENABLED=False`. Setting `AI_API_KEY` alone does not start the AI pipeline or a worker.

Verification after this configuration change:
- `app.config.Settings` load: PASS.
- FAST / REASONING / ESCALATION model currency and family resolution: PASS (USD / Haiku, USD / Sonnet, USD / Opus).
- Focused offline harness/provider/cost-guard tests: **206 passed, 17 skipped, 0 failed**.
- `compileall`: PASS.
- `pip check`: PASS.

No token value was read or written by the assistant. No request was sent to OmniRoute's model endpoint, no paid AI inference occurred, and no worker, Telegram listener, MT5 action, application database migration, or Supabase LIVE mutation was performed. The live-provider canary remains a separate, explicitly approved operation after the operator fills `AI_API_KEY` and confirms the one-call budget/authorization.


## 12. Phase 7.2 — full foreground MOCK process acceptance (2026-11-11)

**Status: COMPLETE — isolated PostgreSQL 16.6 process-level gate passed.**

A fresh PostgreSQL 16.6 cluster was created under the operating-system temporary directory and bound to loopback port 55441 with SSL enabled. The only database used was the throwaway `sireader_test`. The existing app database `sireader_disposable`, production Supabase, and all active application service ports were left untouched.

Test-only boundaries:
- A dedicated `sireader_workforce_runtime_test` login was created in the throwaway cluster. It was NO SUPERUSER, NO CREATEDB, NO CREATEROLE, NO REPLICATION, NO BYPASSRLS and NOINHERIT.
- Test RLS policies and grants limited this login to Haiku and related worker/task/review rows. No test role, password or grant was copied to the application DB or Supabase.
- A temporary read-only API ran on `127.0.0.1:18000` as `sireader_api_ro` over SSL. Its token was process/temp-file scoped and its file ACL was restricted; no secret was written to project files.
- Worker-only switches were set in the worker process environment: runtime enabled, provider mode MOCK, live provider false, per-task USD cap zero, AI pipeline false, DRY_RUN, auto-execution false. No paid provider call or external request occurred.

Acceptance sequence:
1. Seeded one benign, generated, non-sensitive `QUICK_RESEARCH` task explicitly requested for Haiku. The task ID was selected to land in the deterministic Human Review sample.
2. Started a real foreground Windows Python worker process with `python -m app.workers.process --worker haiku --poll-interval 0.5`.
3. The read-only API observed the worker heartbeat as READY while the process was alive.
4. After the isolated test registry row was set to `accepting_tasks=true`, the worker claimed and completed exactly the target task. API-visible task/attempt state was COMPLETED, assigned worker was Haiku, attempt count was 1; activity timeline contained TASK_CLAIMED, TASK_STARTED, HUMAN_REVIEW_QUEUED, TASK_COMPLETED.
5. The attempt had no provider request ID, billing currency or cost. Database inspection confirmed task `result_payload IS NULL`; the review was PENDING and its original output existed only in the quarantine payload table. The API exposed the PENDING review/detail after the read-model owner RLS policy was corrected.
6. Graceful Windows CTRL+C stopped the first worker process. The API reported STOPPED and the process was absent.
7. Restarted a second foreground worker instance with a new instance ID. API reported READY again; graceful CTRL+C returned its heartbeat to STOPPED and the process exited.
8. On the disposable DB only, moved heartbeat timestamps two minutes into the past. The API then reported STALE. This was a deterministic stale-state test, not a two-minute production timeout observation.
9. Stopped the temporary API, gracefully stopped PostgreSQL, verified ports 18000/55441 free, and deleted the exact temporary test directory. No worker process remained.

### Review-view RLS regression found and fixed

The foreground pilot exposed that the legacy local migration `migrations/2026_11_10_workforce_reviews_v1.sql` granted view-owner column SELECT but did not create the required `sireader_view_owner` SELECT policies on `workforce_task_reviews` and `workforce_task_review_payloads`. The base table contained a PENDING row, but the security-barrier API view returned zero rows to the API role.

The local migration now explicitly enables RLS and creates `workforce_view_owner_select` policies on both tables for `sireader_view_owner`, matching the newer Supabase review-read-model draft. The API role still receives SELECT only on the view, not the base tables.

Regression coverage was added to `tests/api/test_postgres_privileges.py`: the fixture now seeds one synthetic PENDING review and payload, asserts the API view exposes that row, and asserts `sireader_api_ro` cannot SELECT either underlying review table. Focused fresh PG16.6 integration result after the change: **22 passed, 0 failed**. This test group is separate from the offline `tests/` run.

Final verification after the migration/test changes:
- Backend `tests/`: **237 passed, 46 skipped, 0 failed** without a disposable DB URL.
- Project Python 3.12.10 compileall: PASS.
- `pip check`: PASS.
- Backend `git diff --check`: PASS.
- The skipped tests require a dedicated disposable PostgreSQL URL; they were not run against `sireader_disposable`.

No long-running worker was left running. The worker test role, temporary API token, test database, temporary PostgreSQL data, certificate/key and logs were all removed with the temporary directory. No app DB or Supabase LIVE object was modified. No paid AI, Telegram, MT5/HFM, trading order, Git commit or push occurred.


## Phase 7.3 preflight hardening — daily USD budget (2026-11-11)

The local Phase 7.3 offline preflight found that USD reservations enforced a per-task cap and rolling 30-day budget, but did not yet enforce an independent daily USD ceiling. A hard daily control was added before any provider canary:

- AI_ANTHROPIC_DAILY_BUDGET_USD is a decimal setting and defaults to 0.00. Zero is a deliberate fail-closed state: _reserve_usd raises BudgetExceededError before writing a reservation or dispatching provider traffic.
- Positive USD daily caps are enforced atomically under the existing PostgreSQL advisory budget lock, using actual plus reserved USD for the local usage date. The per-task cap and rolling 30-day USD budget remain independently enforced.
- get_anthropic_usage now reports daily actual/reserved/projected/remaining USD and explicit enabled/reached flags.
- The read-only telemetry DTO/API now exposes a separate daily_anthropic_usd window. The frontend strict contract, mock fixtures and observability dock display this independently from monthly USD.
- .env.example documents the setting. The user's real .env was not changed and no secret value was read or displayed.

Regression coverage adds a zero-cap fail-closed test, a daily over-cap reservation rejection test and a daily USD usage-report test. The telemetry endpoint test verifies daily USD actual/reserved/projected/cap values independently from monthly usage.

Verification on 2026-11-11:
- Billing + telemetry API tests: 82 passed, 0 failed.
- Backend tests/ suite: 237 passed, 46 skipped, 0 failed.
- Frontend: 422 passed, 0 failed; TypeScript bun run lint passed; production build passed (existing large-bundle warning remains).
- A separate Phase 7 worker/provider offline focus suite previously passed 78/78; no provider HTTP request was made in those tests.

Latest SIREADER preflight values remain fail-closed: provider mode MOCK, worker runtime disabled, live provider switch false, per-task USD cap zero, daily USD cap zero, AI pipeline disabled, trading DRY_RUN, and auto-execution disabled. The gateway key remains configured in SIREADER `.env`; its value was not printed. On 2026-11-11 the owner approved securing and restarting the installed OmniRoute 3.8.52 gateway. Durable `C:\Users\ASUS\.omniroute\.env` now holds `OMNIROUTE_SERVER_HOST=127.0.0.1`, `REQUIRE_API_KEY=true` and the recovered original `STORAGE_ENCRYPTION_KEY`; the key was matched against an existing encrypted DB value and successfully decrypted it. File ACL is restricted to the current user, SYSTEM and Administrators. Verified listener only on `127.0.0.1:20128`; anonymous `GET /v1/models` returned 401 and the SIREADER key returned 200 (4 local catalogue entries). No prompt was submitted and no inference provider was called. Windows Firewall rule inspection remains unavailable, so host firewall exposure is still unverified. The installed-package `.env` no longer contains active bind/auth assignments and npm updates will not remove the durable settings. No paid provider request, worker activation, DB/Supabase cutover, Telegram, MT5/HFM or trading action occurred.

**Phase 7.3 status: PREPARATION HARDENED AND TESTED; LIVE CANARY NOT AUTHORIZED / NOT RUN.** OmniRoute loopback/API-key/encryption preflight is verified. Remaining gates: positive per-task cap, positive daily cap, acceptance of the existing monthly cap, and authorization for exactly one synthetic Haiku request. Windows Firewall exposure remains unverified.


## Phase 7.3 one-shot canary preflight — 2026-11-11

Read-only usage snapshot from Cost Guard:
- 30-day window: 2026-10-13 through 2026-11-11.
- Anthropic monthly hard budget USD 5.00; actual USD 0.00; reserved USD 0.00; projected USD 0.00; remaining USD 5.00.
- Anthropic daily hard cap effective USD 0.00 (fail-closed); daily actual/reserved USD 0.00.
- Active FAST model: \`anthropic/claude-haiku-5-5\`; Haiku worker profile max output: 700 tokens.
- Cost Guard's offline estimate at 2,000 model-counted input tokens and 700 output: estimated USD 0.000750, reserved USD 0.001125 (1.5× safety factor). At 10,000 input tokens and 700 output: reserved USD 0.003525. No token-count API or inference endpoint was called for these estimates.

**Proposed, not configured:** per-task reserved-cost cap USD 0.01; daily actual+reserved cap USD 0.01; retain monthly cap USD 5.00. A cap applies to the Cost Guard reservation estimate; it is not an absolute guarantee of the upstream invoice if billing differs.

The proposed live test will use a fresh disposable PostgreSQL 16.6 database, one benign synthetic task, and a direct single invocation of \`run_worker_once("haiku", ...)\`—never the forever-polling worker CLI. AI pipeline stays off; only the isolated one-task process would receive temporary LIVE worker environment switches. Trading remains DRY_RUN; auto-execution remains disabled. No retries are made; UNKNOWN outcome means stop and reconcile.

Current authorization: **NOT APPROVED / NOT RUN**. Owner has agreed to prepare Phase 7.3 only. Await acceptance of monthly USD 5.00, the proposed per-task/daily USD 0.01 caps (or different values), and explicit approval for exactly one paid synthetic Haiku request. No positive caps or live flags have been written to the owner's \`.env\`.


## Phase 7.3 — LIVE Haiku canary execution evidence — 2026-11-11

**PASS: one and only one successful provider inference request.** Owner authorization covered per-task USD 0.01, daily USD 0.01, monthly USD 5.00, and one synthetic paid Haiku request.

- Disposable PG16.6 SSL cluster, loopback port 55442; target DB `sireader_test`; exactly one synthetic QUICK_RESEARCH task requested for worker `haiku`, `max_attempts=1`, no other worker accepting tasks.
- One successful `run_worker_once` completion, one attempt, and event chain: TASK_ENQUEUED, TASK_CLAIMED, TASK_STARTED, PROVIDER_REQUESTED, TASK_COMPLETED. Output present (86 characters); no pending review.
- Independent OmniRoute `usage_history`: `POST /v1/messages`, HTTP 200/success, `claude-haiku-5-5`, key label `SIREADER_CLAUDE`, 249 input / 33 output tokens, zero cache tokens, timestamp 2026-11-11T09:46:19.645Z. Exactly one matching successful row.
- Cost Guard temporary ledger: one COMPLETED USD row, actual USD 0.000042, reserved USD 0.000000; daily cap USD 0.01, monthly ceiling USD 5.00, per-task cap USD 0.01. The amount is token-price accounting rounded to six decimals ($0.0000414 raw), not a separately provider-reported invoice; OmniRoute's provider_cost_usd was NULL.
- The global application ledger was read-only before/after and remains actual/reserved USD 0/0; no Supabase mutation. Record USD 0.000042 separately for future budget reconciliation because the disposable ledger is removed after cleanup. Global daily cap remains zero/fail-closed.
- Initial pre-provider claim failed because WorkActivityEventRecord lacked ORM FKs matching the DB audit table. Fixed task_id/attempt_id FK mappings and explicitly flushed the attempt before TASK_CLAIMED. PG16.6 claim/rollback probe passed; worker tests after fix: 97 passed, 17 skipped; compileall and diff check passed. First failed invocation had zero attempts/usage and made no provider call; no retry followed an uncertain outcome.
- Runtime flags in persistent .env remain OFF/MOCK/zero; the process used one-shot environment overrides only. Trading remains DRY_RUN; auto execution OFF.

The canary passes Phase 7.3. It does **not** authorize Phase 7.4 rollout, additional live requests, or production Supabase changes. Virtual Office v1.0 remains NOT RELEASED. The exact cost must be carried manually in future Cost Guard budget reconciliation because no global ledger mutation was made.


## Phase 7.4 global provider gate — offline implementation (2026-11-11)

The worker runtime now contains a database-coordinated global LIVE provider slot, initially hard-fixed to one request across Haiku, Sonnet and Opus. The PostgreSQL migration `2026_11_11_workforce_global_provider_gate_v1.sql` creates a SECURITY DEFINER gate function owned by non-login `sireader_view_owner`. The migration and rollback are guarded to database `sireader_test`; execution is granted to the worker runtime's local `postgres` login and denied to the separate Command API role, API read-only role, and PUBLIC.

`mark_provider_requested` itself acquires the slot and persists the task attempt as `PROVIDER_REQUESTED` in the same transaction under a transaction-scoped PostgreSQL advisory lock. This enforcement lives in the repository method, not only in the current runtime callback. The attempt status is the durable slot record. `PROVIDER_REQUESTED` and `UNKNOWN` block another provider request; UNKNOWN has no time-based expiry and requires explicit reconciliation. A rejected pre-dispatch gate is recorded as definitely-not-charged and retryable, without sending the paid Messages request.

Offline verification on 2026-11-11: worker/provider focus **81 passed**; backend `tests/` **251 passed, 47 skipped**; full root-level backend suite with the existing project virtual environment **628 passed, 47 skipped, 0 failed**; compileall and `git diff --check` passed. PostgreSQL integration tests for concurrency, RLS/ACL and rollback/forward were added but not run because `SIREADER_TEST_POSTGRES_URL` is unset and the available PG18.6 installation lacks `share/postgres.bki`. The attempted disposable cluster was not started; no populated database was used.

The system Python interpreter lacks `MetaTrader5` and initially failed collection in nine legacy MT5/Telegram-related modules. The existing `C:\SIREADER\.venv` already contains that dependency; using it, the full root-level backend suite completed **628 passed, 47 skipped, 0 failed** without installing packages. The 47 skips include PostgreSQL integration cases requiring an explicitly configured disposable `SIREADER_TEST_POSTGRES_URL`.

No LIVE worker, further paid provider request, Supabase migration, Telegram, MT5/HFM or trading action was performed. Persistent .env settings remain OFF/MOCK/zero and trading remains DRY_RUN. Phase 7.4 LIVE rollout is still BLOCKED pending successful disposable-PostgreSQL verification and separate explicit owner authorization. Virtual Office v1.0 remains NOT RELEASED.


### Root-level regression verification — interpreter correction (2026-11-11)

The system Python interpreter lacks the optional MetaTrader5 package and initially failed collection in nine legacy MT5/Telegram-related modules. The existing `C:\SIREADER\.venv` already contains MetaTrader5, so the full root-level backend suite was rerun using that interpreter without installing packages: **628 passed, 47 skipped, 0 failed**. The 47 skips include PostgreSQL integration cases that still require an explicitly configured disposable `SIREADER_TEST_POSTGRES_URL`.


## Phase 7.4 — PostgreSQL integration verification — 2026-11-11 (latest)

This addendum supersedes prior statements that the integration tests were not run.

A fresh EDB PostgreSQL 18.6 binary archive was checksum-verified and extracted to a temporary user directory. A disposable `sireader_test` database was started on loopback port 55442 only. The two PostgreSQL integration modules completed **24 passed, 0 skipped, 0 failed**. Coverage exercised migration idempotency, rollback/forward, SECURITY DEFINER owner and grants, Command API/API read-only/PUBLIC denial, view-owner RLS, UNKNOWN quarantine, and concurrent Haiku/Sonnet provider-slot reservation (exactly one winner).

The full backend root suite then completed **675 passed, 0 skipped, 0 failed** using the existing `C:\SIREADER\.venv`. Python compileall passed. One non-blocking Starlette/httpx deprecation warning remains.

The temporary PostgreSQL server was stopped, port 55442 was confirmed closed, and all downloaded/extracted test binaries plus test data were removed. No installed service, production database, or Supabase connection was used.

Persistent runtime remains OFF/MOCK, live provider OFF, per-task and daily caps zero, monthly ceiling USD 5.00, and trading DRY_RUN with auto execution disabled. The previous one-shot Haiku canary remains the only paid request; preserve USD 0.000042 as manual price-based budget reconciliation. No further provider request or LIVE worker launch is authorized by this verification.

**Phase 7.4 code and PostgreSQL technical verification: PASS. Limited LIVE rollout remains blocked pending fresh specific owner authorization. Supabase production cutover remains separate. Virtual Office v1.0 remains NOT RELEASED.**


## Production worker DB boundary — fail-closed implementation (2026-11-11)

The worker runtime no longer imports the core application's global `SessionLocal` directly. New `app.workers.database` provides `WorkerSessionLocal` and validates a dedicated `WORKER_DATABASE_URL` before a non-MOCK/live worker can heartbeat, claim, or dispatch work.

- Accepted login: `sireader_worker_runtime` (direct) or `sireader_worker_runtime.<project-ref>` (pooler-style).
- Remote URL requires TLS mode `require`, `verify-ca`, or `verify-full`.
- Empty URL is permitted only for deterministic MOCK mode with the live-provider switch OFF. It then lazily uses the existing local/core session factory.
- Broad `postgres`, `sireader_api_ro`, `sireader_workforce_cmd`, other role identities, non-PostgreSQL URLs, malformed URLs, and remote URLs without TLS fail closed. Errors do not reveal URL, host, or credential material.
- The existing private backend `.env` was not changed; `WORKER_DATABASE_URL` remains empty/unconfigured. No production worker role was created or granted privileges.

Verification after the guard change:
- Worker DB boundary + runtime focus: **27 passed**.
- Full backend suite with a fresh disposable PostgreSQL 16.6 `sireader_test` cluster: **690 passed, 1 skipped, 0 failed**.
- This full suite includes migration/idempotency, rollback/forward, ACL/RLS, UNKNOWN quarantine, and concurrent global provider-slot tests.
- `compileall`: PASS; `git diff --check`: PASS.
- Test cluster stopped; port 55444 confirmed closed; temp directory removed.

### Remaining production writer design

The guard deliberately does not create production DDL or make a PostgreSQL identity usable. A separately reviewed role/migration must still implement row/column RLS and database-enforced worker lifecycle limits. The current repository writes through ORM CRUD across tasks, attempts, heartbeats, audit events, and conditional review payloads. In addition, `run_worker_once` still runs lease recovery, expiry, stale-review and payload-purge maintenance work; production worker rights must not be broadened to absorb that maintenance. A dedicated maintenance boundary and trusted result-release path are still required.

See `docs/PRODUCTION_WORKER_DATABASE_IDENTITY_DESIGN_2026-11-11.md`. Do not apply local-only maintenance/provider-gate migrations to Supabase LIVE. No additional provider request, worker activation, Supabase mutation, Telegram, MT5/HFM, or trading action occurred. Phase 7.4 technical verification is PASS; live rollout remains blocked; v1.0 NOT RELEASED.


## Final worker database guard closeout — 2026-11-11

The worker database boundary is now stricter than the initial implementation note above:

- If `WORKER_DATABASE_URL` is absent, the worker can use legacy core `SessionLocal` only in MOCK mode with live provider OFF **and** only when `DATABASE_URL` is an explicit loopback PostgreSQL target naming `sireader_disposable`, `sireader_test`, or the dummy unit-test DB. A remote core URL is rejected even in MOCK mode.
- The dedicated URL requires the `sireader_worker_runtime` login, an explicit DB name, no URL fragment, and no libpq target-override parameters such as host/hostaddr/service. Only one `sslmode` value is allowed; remote requires require/verify-ca/verify-full. Connect timeout is 10 seconds and pool size is bounded.
- Worker configuration errors are raised before heartbeat, claim, or provider dispatch and do not echo URLs/credentials.

Final verification after the complete guard hardening:
- Focused worker DB guard/runtime: **42 passed**.
- Full backend suite using fresh disposable PostgreSQL 16.6: **702 passed, 1 skipped, 0 failed**.
- Python compileall and backend `git diff --check`: PASS.
- PostgreSQL stopped and temporary cluster removed; port 55448 closed.

No private `.env` value changed. `WORKER_DATABASE_URL` remains unconfigured; no production writer role or grants were created. Production maintenance/review-release boundary and staging migration rehearsal remain open. Persistent runtime remains OFF/MOCK, live provider OFF, costs zero, and trading DRY_RUN.

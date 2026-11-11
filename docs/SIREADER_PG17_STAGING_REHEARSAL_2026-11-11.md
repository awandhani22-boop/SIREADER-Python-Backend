# SIREADER — PostgreSQL 17.11 Local Staging Rehearsal
Date: 2026-11-11 (Asia/Jakarta)

Status: **READ-MODEL / API STAGING ACCEPTANCE PASS — PRODUCTION WORKER WRITER AND CUTOVER STILL BLOCKED**

## 1. Scope and safety

This rehearsal was performed on the user's Windows workstation using a disposable PostgreSQL 17.11 binary distribution extracted into the user's staging folder. It did not install a Windows service or require Administrator privileges.

- PostgreSQL: 17.11, EDB Windows x64 binaries package revision 5.
- Archive: `postgresql-17.11-5-windows-x64-binaries.zip`.
- Official SHA-256 sidecar and local archive hash both matched:
  `80379b2c04d51c30225532e0ae04509899141e9957ed096fe749d7fd9df8f82f`.
- Extracted under `C:\SIREADER-STAGING\pg17\pgsql`.
- Production-like staging database: `sireader_stage`, loopback port 55439 only, TLS enabled with a local self-signed certificate.
- Separate disposable privilege-test database: `sireader_test`, loopback port 55440 only.
- Source backup: `C:\SIREADER_BACKUP_20261110\supabase-production-20261111\sireader_supabase_production_2026-11-11.dump`.
- Backup SHA-256 re-used from the verified manifest:
  `FC83D455703EC0A66BC656EFCC82D6D9D6513AFFDCFF94C96C63CCEEE2A12333`.

The backup file was read only. The restore selected the core `auth`, `public`, `extensions`, and `supabase_migrations` schemas with `--no-owner --no-acl`. Staging-only Supabase roles, six legacy Phase 3.1 view owners, and the minimum legacy view-owner column grants were then reconstructed locally to match the reviewed API read boundary. This is not a full Supabase platform clone or full disaster-recovery acceptance.

## 2. Production migration drafts

Applied to `sireader_stage` in timestamp order using PostgreSQL 17.11 and `psql -v ON_ERROR_STOP=1`:

1. `20261110223523_workforce_runtime_schema_v1.sql` — PASS.
2. `20261110223529_workforce_observability_views_v1.sql` — PASS.
3. `20261110223534_workforce_review_read_model_v1.sql` — PASS.
4. `20261111010428_phase_3_1_security_barrier_hardening.sql` — PASS.

The fourth migration initially rejected the restored baseline because `pg_restore --no-owner` had intentionally omitted the six legacy view owners. The staging-only baseline was reconciled to `sireader_view_owner`, then the unchanged migration was rerun successfully. No migration source was weakened to bypass its precondition.

## 3. Database security audit

The backend's actual `PRIVILEGE_AUDIT_SQL` and validation functions were executed with `current_user = sireader_api_ro`, a read-only transaction, and a TLS-required connection.

Results:

- Dedicated role identity and least-privilege attributes: PASS.
- `transaction_read_only=on`: PASS.
- Schema USAGE: true; schema CREATE: false.
- Approved API views: 14/14 present.
- Approved view owners: 14/14 `sireader_view_owner`.
- Approved view SELECT grants: 14/14.
- Approved view `security_barrier=true`: 14/14.
- Approved view execution probes: 14/14 PASS.
- API view write and column-write access: false.
- API base-table table/column access and MAINTAIN: false.
- PUBLIC table/column grants on protected objects: false.
- View owner schema CREATE, base-table table/maintain access, role memberships, and unapproved column access: false.
- View-owner column allowlist audit: PASS.
- Public base tables: 24; RLS-enabled base tables: 24/24.
- TLS session probe: TLSv1.3, `TLS_AES_256_GCM_SHA384`.

The exact startup guard ended with `API_READONLY_STARTUP_AUDIT_PASS`.

## 4. API and SSE smoke tests

Fourteen authenticated GET endpoints returned HTTP 200 against the real PostgreSQL 17.11 staging database:

- system status, telemetry usage, active pipeline, active market watches, audit decisions;
- workforce runtime, tasks, attempts, activity, task timeline, pending reviews, review detail;
- governance pending reviews and governance review detail.

Unauthenticated system-status request returned HTTP 401 as expected.

The TestClient's Windows `ProactorEventLoop` produced the known SSE listener retry during its test lifecycle. A separate explicit `WindowsSelectorEventLoop` listener test connected over TLS, passed the trigger inventory checks, and reported `LISTENER_READY=True`.

Only synthetic staging task/attempt/event/review rows were added to `sireader_stage`. They are labelled SIMULATED and are not production data.

## 5. Automated verification

- Full backend suite with live database URLs unset for the unit run: **657 passed, 47 skipped, 0 failed**.
- PostgreSQL 17.11 disposable privilege integration suite: **23 passed, 0 failed**.
- Python `compileall -q app tests`: PASS.
- Backend `git diff --check`: PASS.
- Virtual Office frontend tests: **422 passed, 0 failed**.
- Frontend TypeScript check (`npm run lint`): PASS.
- Frontend production build (`npm run build`): PASS.
- Vite reports a non-blocking bundle-size warning: main JS 1,511.12 kB minified / 405.35 kB gzip. Consider code-splitting in a separate performance task.

## 6. Explicitly not accepted by this rehearsal

This is a read-model, API, TLS, and privilege-boundary rehearsal only. It does NOT validate or authorize:

- a production worker runtime login or database writer functions;
- database command functions, maintenance functions, or review-operator identity;
- replacement of direct ORM mutations with a production-safe function adapter;
- lease-to-attempt provenance, transaction-scoped review creation, or trusted result release under a production worker role;
- concurrent provider-slot behavior under the proposed production writer identity;
- full Supabase Auth/Storage/Realtime/Vault/GraphQL/managed-extension restore;
- production migration, API URL cutover, worker enablement, paid provider calls, or LIVE rollout.

The static writer-security review and command contract remain active blockers. Production writer SQL and application adapter conversion are not implemented by this rehearsal.

## 7. Safe operating state

No production Supabase DDL/DML, role creation, credential rotation, remote API URL change, or production migration was performed. The existing `.env` was not modified and its production database URLs were not used by the staging scripts. `WORKER_DATABASE_URL` remains unconfigured.

- AI pipeline: OFF.
- Worker runtime: OFF.
- Provider mode: MOCK.
- Live provider: OFF.
- Per-task worker cost cap: 0.
- Trading mode: DRY_RUN.
- Auto execution: OFF.
- No paid AI request, Telegram action, MT5/HFM action, or trade execution.

**Decision:** PostgreSQL 17.11 read-model/API staging acceptance PASS. Production worker writer, production cutover, and SIREADER Virtual Office v1.0 remain NOT READY / NOT RELEASED.


## 8. Staging shutdown and preservation

After all acceptance checks, both PostgreSQL clusters were stopped cleanly. Ports 55439 and 55440 have no listeners. Their `pg_hba.conf` loopback host rules were changed from temporary trust to REJECT before shutdown, so an accidental restart does not reopen the test databases. Staging data, logs, TLS certificate, and verified PostgreSQL binaries remain on disk under `C:\SIREADER-STAGING\pg17` and `C:\SIREADER-STAGING\pg17-test` for an explicitly resumed rehearsal. No Windows service was registered.

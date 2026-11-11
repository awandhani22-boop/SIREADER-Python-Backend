# Phase 7.4 — Production Migration and Authority Rehearsal Closeout

Date: 2026-11-11 (Asia/Jakarta)
Scope: local PostgreSQL 17.11 restore of the Supabase production backup; forward/rollback/forward rehearsal; worker/command/review/maintenance authority; Python identity validation.
Decision: **LOCAL PRODUCTION-MIGRATION REHEARSAL PASS — SUPABASE HOSTED CUTOVER STILL BLOCKED / NOT AUTHORIZED.**

This is a Phase 7.4 closeout supplement. It does not create a new roadmap phase and does not authorize a hosted Supabase migration, production credentials, worker activation, further paid inference, or a v1.0 release.

## 1. Hosted production baseline observed

The connected Supabase project was inspected read-only:

- PostgreSQL 17.11, project region `eu-central-1`, status ACTIVE_HEALTHY.
- Four migration-history entries were recorded in the hosted migration ledger; they cover the earlier Phase 3.1 read-only observability work.
- The hosted public schema contained 15 base tables and six `v_api_*` views.
- The six existing observability views were owned by `sireader_view_owner`.
- No workforce base tables, `sireader_workforce_private` schema, worker LOGIN roles, Command API role, trusted review role, or maintenance role were installed.
- The hosted read-only baseline had not yet been extended to the 14-view workforce observability contract.

No production schema, role, password, grant, RLS policy, function, view, setting, or migration history was changed during this review.

## 2. Production backup and clean rehearsal

The existing local custom-format backup was used:

- File: `C:\SIREADER_BACKUP_20261110\supabase-production-20261111\sireader_supabase_production_2026-11-11.dump`
- Size: 495,806 bytes
- SHA-256: `FC83D455703EC0A66BC656EFCC82D6D9D6513AFFDCFF94C96C63CCEEE2A12333`

A new isolated PostgreSQL 17.11 cluster was initialized at `C:\SIREADER-STAGING\pg17-cutover`, bound to IPv4 loopback port 55450. A new database `sireader_cutover_rehearsal_20261111` was restored from the backup. The existing six API views were assigned to the same `sireader_view_owner` identity used by the hosted baseline; no security-barrier guard was weakened.

The rehearsal applied, in order:

1. `20261110223523_workforce_runtime_schema_v1.sql`
2. `20261110223529_workforce_observability_views_v1.sql`
3. `20261110223534_workforce_review_read_model_v1.sql`
4. `20261111010428_phase_3_1_security_barrier_hardening.sql`
5. `20261111170344_workforce_database_authorities_v1.sql`

All five files applied successfully to the new restored database. The final production migration guard verified the prerequisite Phase 3.1 identities, nine RLS workforce base tables and fourteen security-barrier API views before creating the authority boundary.

## 3. Production authority migration and rollback

New forward migration:

- `supabase/migrations/20261111170344_workforce_database_authorities_v1.sql`

New rollback companion:

- `supabase/rollback/20261111170344_workforce_database_authorities_v1.rollback.sql`

The forward migration creates ten distinct LOGIN identities: seven worker classes plus Command API, trusted Review Operator and Maintenance. It does not embed or set any password. Every role is NOINHERIT, NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOREPLICATION and NOBYPASSRLS. A hardened NOLOGIN function owner owns the private SECURITY DEFINER boundary. The migration installs the previously staged eight worker lifecycle functions, four command functions, two review functions, one bounded maintenance entry function and their private helper/audit structures.

The database final audit passed:

| Check | Result |
|---|---:|
| Public base tables in the restored schema | 24 |
| Workforce base tables with RLS | 9 / 9 |
| API views with `security_barrier=true` | 14 / 14 |
| Distinct worker/command/review/maintenance LOGIN roles | 10 |
| LOGIN roles without a password hash at migration time | 10 / 10 |
| Role memberships granted to those LOGIN roles | 0 |
| Direct SELECT/INSERT/UPDATE/DELETE grants on workforce base tables to those LOGIN roles | 0 |
| PUBLIC EXECUTE on private workforce functions | 0 |
| Worker tasks, attempts, reviews, heartbeats and activity events in the clean rehearsal | 0 |

The rollback script removes only the Phase 7 private authority schema and its dedicated roles/policies. It preserves all nine workforce schema tables, their RLS state and the fourteen read-only API views. It refuses to run if worker operational data exists or if a workforce LOGIN session is still connected. The rollback completed successfully against the empty rehearsal. The forward migration then reapplied successfully to the same restored database, proving the sequence **forward → rollback → forward**.

The rollback is deliberately not a data-deletion tool. Once operational tasks, attempts, reviews, heartbeats or events exist, it refuses; use a forward repair/recovery plan instead.

## 4. Python role identity and application integration

The Python Command API now detects the complete PostgreSQL function boundary and uses it instead of ORM writer calls for enqueue, cancel, retry and worker scheduling. A partial function installation fails closed. The trusted review route uses a separate review database pool and requires the `sireader_workforce_review` identity; the legacy `X-Operator-Id` header is not accepted as database authority in function mode.

The maintenance runner has a separate database-function mode. Its URL must use the dedicated `sireader_workforce_maint` role, explicit database, psycopg 3 and TLS for remote connections. It rejects connection-routing overrides, unsafe PostgreSQL environment overrides, direct workforce table privileges, missing RLS, unsafe function owner/search_path, and a caller-supplied maintenance timestamp. The existing ORM maintenance path remains loopback-only.

Verification was performed using actual database LOGIN session identities on the local rehearsal clone (the local-only HBA trust rule was limited to IPv4 loopback; no password was provisioned). The application validated `current_user` and `session_user` separately for Command, Review and Maintenance; enqueue/replay/cancel/pause/resume and an empty bounded maintenance call ran inside transactions that were explicitly rolled back.

Acceptance evidence:

- `STAGING_WORKER_COMMAND_LAYER_TESTS_PASS`
- `STAGING_OPERATOR_REVIEW_MAINTENANCE_COMMAND_LAYER_TESTS_PASS`
- `FASTAPI_PG17_FUNCTION_COMMAND_REVIEW_SMOKE_PASS`
- `REAL_LOGIN_COMMAND_REVIEW_MAINTENANCE_PG17_PASS`
- Focused Python regression set: **84 passed**
- Full backend `tests/` suite: **327 passed, 47 skipped, 0 failed**
- `compileall -q app`: PASS
- Virtual Office official Node/tsx test command: **422 passed**
- Virtual Office TypeScript/lint: PASS
- Virtual Office production build: PASS

The 47 skipped backend tests require `SIREADER_TEST_POSTGRES_URL`; they are not represented as passing database tests. The SQL command layer, policy and adapter tests were run explicitly against the separate PostgreSQL 17.11 rehearsal. The Virtual Office build retains the previously documented non-blocking large-bundle warning (1,511.12 kB minified / 405.35 kB gzip).

## 5. Safety and remaining hosted-production gates

The following state was not changed:

- AI pipeline OFF.
- Worker runtime OFF/MOCK.
- Live provider OFF.
- `WORKER_DATABASE_URL` unset.
- `SIREADER_WORKFORCE_REVIEW_DATABASE_URL` unset in the real application environment.
- `SIREADER_WORKFORCE_MAINTENANCE_FUNCTION_DATABASE_URL` unset in the real application environment.
- Per-task and daily USD limits remain zero/fail-closed.
- Trading remains DRY_RUN; automatic execution remains OFF.
- No paid inference, Telegram listener/action, MT5/HFM connection, trading order or provider dispatch occurred.

Hosted Supabase cutover remains blocked by explicit items:

1. The hosted target still has only the previously observed 15 base tables and six API views; the local workforce baseline and five-file migration rehearsal have not been applied there.
2. Dedicated production secrets/passwords for the ten LOGIN identities have not been provisioned. The migration intentionally leaves passwords unset; credential provisioning and rotation must happen through a separately controlled secret process, never in SQL committed to Git.
3. The local PostgreSQL restore does not certify Supabase-managed PostgREST/Data API exposure, pooler identity normalization, network policy, platform defaults or the actual hosted role topology. A separately approved managed-Supabase-equivalent staging rehearsal is still required.
4. A production change-window backup/restore point, independent restore rehearsal, rollback operator and monitoring/incident plan must be approved for the actual hosted target.
5. The read-only API and Command API connection targets must be reconciled before starting the complete service stack.
6. The Phase 7.3 one-request Haiku authorization is consumed. Any further paid LIVE worker request requires a new explicit bounded authorization (worker/model, exact request count, synthetic task, budget and hard-stop window).
7. Owner operational/browser acceptance and v1.0 release sign-off remain outstanding.

**Disposition:** Phase 7.4 production migration design and local restored-backup rehearsal PASS. Hosted Supabase deployment and limited LIVE rollout remain BLOCKED / NOT AUTHORIZED. **SIREADER Virtual Office v1.0 remains NOT RELEASED.**

Do not weaken the production migration preconditions, apply this migration chain to Supabase LIVE, create hosted credentials, set LIVE flags, or start a paid worker as part of this rehearsal.



## Managed-role and actual-LOGIN HTTP continuation — 2026-11-12

This continuation supersedes the earlier rehearsal wording where the first local PG17 test used a superuser identity. The final authority migration was reapplied successfully on a clean restore using a separate **NOSUPERUSER** migration operator with only the managed-like CREATEROLE/CREATEDB/BYPASSRLS capabilities. Hosted Supabase remained read-only and unchanged.

### Migration and permission evidence

- The four previously-applied hosted migration SQL files were restored exactly from the hosted `supabase_migrations.schema_migrations` ledger into `supabase/migrations`. The five Phase 7 workforce/security migrations follow them in local version order. No historic migration was applied to hosted again.
- Supabase CLI 3.1.0 generated the authority migration filename; `supabase init` generated a local `config.toml`. No project link, password, or secret was written.
- The final authority migration passed on local PG17.11 as a LOGIN operator with `NOSUPERUSER`, `CREATEROLE`, `CREATEDB`, `BYPASSRLS`, mirroring the inspected Supabase `postgres` role shape.
- Effective permission matrix: all seven worker identities have all seven lifecycle functions; provider-slot reservation is EXECUTE only for Haiku/Sonnet/Opus; Command has four functions; Review has two; Maintenance has one.
- Final audit after COMMIT: zero PUBLIC EXECUTE on the private schema; zero direct workforce base-table privileges on worker/command/review/maintenance runtime LOGINs; nine workforce tables with RLS; fourteen API views with `security_barrier=true`; function owner retains USAGE but not CREATE on its private schema.
- The ten runtime LOGIN roles do not inherit membership in other roles. PostgreSQL CREATEROLE leaves the migration operator's creator ADMIN membership on the NOLOGIN function-owner role; the migration explicitly removes its temporary SET edge and final verification reports `set_option=false`, `inherit_option=false`. It therefore cannot SET ROLE or inherit function-owner privileges after migration.

### Runtime acceptance

- SQL worker acceptance: `STAGING_WORKER_COMMAND_LAYER_TESTS_PASS`.
- SQL operator/review/maintenance acceptance: `STAGING_OPERATOR_REVIEW_MAINTENANCE_COMMAND_LAYER_TESTS_PASS`.
- Actual PostgreSQL LOGIN startup/adapter/maintenance test: `REAL_LOGIN_COMMAND_REVIEW_MAINTENANCE_PG17_PASS`.
- Actual LOGIN FastAPI HTTP test: `REAL_LOGIN_FASTAPI_COMMAND_REVIEW_PG17_PASS`. Enqueue, idempotent replay, cancel, worker pause/resume and review approval were exercised through HTTP with distinct Command and Review logins. An intentionally untrusted `X-Operator-Id` header did not override the actor recorded from the Review database identity.
- Synthetic review fixtures were rejected/expired/purged through the authenticated Review and Maintenance function boundaries. No retention trigger was bypassed.

### Latest regression

- Backend: **327 passed, 47 skipped, 0 failed**. The 47 skips require the separate `SIREADER_TEST_POSTGRES_URL`; explicit PG17 SQL and actual-LOGIN acceptance ran separately.
- Virtual Office: **422 passed, 0 failed**; TypeScript/lint passed; production build passed.
- Non-blocking warnings: Starlette/httpx TestClient deprecation, React `act(...)` notices, Three.js CommonJS deprecation, Vite JS bundle 1,511.12 kB minified / 405.35 kB gzip.

### Local CLI and hosted-platform boundary

- The historical hosted migration ledger was reconciled into the local repo. CLI 3.1.0 `migration list --remote=<project-ref>` could not connect from this workstation because its current network does not support IPv6 and the CLI requires an explicit pooler URL for IPv4. The ledger itself was read and verified through the Supabase database connection.
- Supabase CLI local configuration is initialized. No hosted `db push`, `db pull`, migration, role, credential, or setting change was run.
- **Owner decision (2026-11-12):** skip/waive the Supabase development-branch / managed-equivalent staging step to avoid the quoted **US$0.01344/hour** compute charge. No branch was created and no branch compute is running. This is a SKIPPED validation gate, not a managed-Supabase staging PASS or certification.

**Current disposition:** Phase 7.4 implementation, SQL, Python adapters, managed-like non-superuser migration/rollback rehearsal, actual LOGIN and HTTP acceptance are locally complete. The owner has explicitly skipped/waived hosted Supabase equivalent-staging to avoid recurring branch compute; this gate is SKIPPED, not PASSED. Production deployment remains NOT AUTHORIZED pending verified hosted backup/recovery, approved change window and rollback operator, production secret provisioning, and owner sign-off. No password was provisioned, no paid provider request or worker was activated, and v1.0 remains NOT RELEASED.

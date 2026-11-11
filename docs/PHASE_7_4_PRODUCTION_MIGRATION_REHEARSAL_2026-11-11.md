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
5. `20261111235000_workforce_database_authorities_v1.sql`

All five files applied successfully to the new restored database. The final production migration guard verified the prerequisite Phase 3.1 identities, nine RLS workforce base tables and fourteen security-barrier API views before creating the authority boundary.

## 3. Production authority migration and rollback

New forward migration:

- `supabase/migrations/20261111235000_workforce_database_authorities_v1.sql`

New rollback companion:

- `supabase/rollback/20261111235000_workforce_database_authorities_v1.rollback.sql`

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

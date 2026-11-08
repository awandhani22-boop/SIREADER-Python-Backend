# Phase 3.3 — Owner Supabase and Chrome Acceptance Runbook

**Status:** REQUIRED OWNER ACCEPTANCE — not yet performed against the owner's Supabase project  
**Backend branch:** phase-3-3-sse-backend  
**Virtual Office branch:** phase-3-3-sse  
**Safety:** This runbook does not authorize trading actions or exposure of credentials.

## 1. Before touching the database

1. Confirm both implementation branches and the intended commit SHAs with the current execution log.
2. Ensure the Python Backend uses the dedicated SIREADER_API_DATABASE_URL for the sireader_api_ro role. Do not use DATABASE_URL or a database owner account for the listener.
3. The owner's previous environment used the Supabase Session Pooler on port 5432 because the direct DB host did not have a usable IPv4 route. Confirm this connection is a session-preserving pooler mode; do not use transaction pooling for a long-lived LISTEN session.
4. Never print or paste the database URL, password, API token, cookie, Authorization header or environment contents.

## 2. Test the persistent LISTEN connection without changing rows

From the Python Backend repository root, in one PowerShell terminal:

```powershell
python -m app.api.event_listener_probe --wait-seconds 90
```

The probe reads SIREADER_API_DATABASE_URL via the existing API configuration, connects with autocommit, executes LISTEN on the fixed channel, and waits for one allowlisted notification. It prints no DSN or credentials.

Expected initial output:

```text
LISTEN_READY
```

In a separate Supabase SQL Editor session using the approved database-owner connection, run a harmless notification (no row mutation):

```sql
NOTIFY sireader_observability_v1, 'system';
```

After the SQL Editor statement commits, the probe should print:

```text
NOTIFICATION_RECEIVED resource=system
```

Interpretation:
- LISTEN_READY + NOTIFICATION_RECEIVED proves the active configured route can hold a listener session and receive a notification during this test.
- LISTEN_READY + NO_NOTIFICATION_WITHIN_TIMEOUT means the session opened, but delivery was not demonstrated; check the actual pooler mode and that the notification was committed from a separate connection.
- LISTEN_PROBE_FAILED reports only an exception class; do not turn on verbose DSN logging.
- The probe does not write to database tables.

## 3. Review and apply migration (owner controlled)

Only after the owner approves the DB change, apply:

- migrations/2026_11_09_phase_3_3_observability_notify.sql

Use the approved migration owner/DBA identity (normally the project database owner), never sireader_api_ro. Do not add write privileges to sireader_api_ro.

The migration installs static resource-key notifications on the six observability source tables. Approval decisions are append-only and notify on INSERT only. UPDATE comparisons inspect only fields used by the public snapshots; raw row contents are never included in NOTIFY payloads.

Verify the trigger catalog after application:

```sql
SELECT
    n.nspname AS schema_name,
    c.relname AS table_name,
    t.tgname AS trigger_name,
    pg_get_triggerdef(t.oid) AS trigger_definition
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND t.tgname = 'sireader_observability_notify'
  AND NOT t.tgisinternal
ORDER BY c.relname;
```

Expected: six non-internal triggers on runtime_control_state, ai_usage_logs, ai_analysis_logs, ai_pending_messages, trade_decision_watches, approval_decisions. Verify that the approval_decisions trigger is INSERT-only. The runtime role must retain no base-table write, TRIGGER, or migration privileges.

Check the function security properties:

```sql
SELECT
    p.proname,
    p.prosecdef AS security_definer,
    p.proconfig
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
  AND p.proname = 'sireader_notify_observability_change';
```

Expected: security_definer is false; search_path is fixed to pg_catalog, public.

If migration verification fails or the stream becomes unsafe, use the paired rollback script:

migrations/rollback/2026_11_09_phase_3_3_observability_notify_rollback.sql

Rollback must be executed only by the approved database owner. Never improvise trigger/function changes in the production SQL editor.

## 4. Start API and Virtual Office

Start the Python API using its existing Phase 3.1 runbook. Start Virtual Office on the Phase 3.3 branch using the existing launcher instructions. Do not start an unreviewed production process or change secrets.

The API may report the snapshot service healthy while the event listener is degraded. In that case the SSE endpoint should return a sanitized 503 before stream headers and the frontend must continue using snapshot polling.

## 5. Chrome verification

Open exactly:

http://127.0.0.1:3000

After operator login:
- Open DevTools Network and filter for events/stream.
- Confirm one same-origin GET to /api/v1/events/stream with status 200 and content type text/event-stream when listener readiness is true.
- Confirm the browser request does not contain Authorization: Bearer and no token appears in query string, bundle, local/session storage or logs.
- Browser session cookie remains HttpOnly, Secure, SameSite=Strict, Path=/ and host-only; document.cookie cannot read it.
- EventStream status should show connected while established; snapshot cards keep their own LIVE/STALE/OFFLINE/ERROR status.

From a separate SQL Editor connection, send the harmless NOTIFY test from section 2. Confirm the corresponding snapshot refreshes and SIMULATED state is unchanged. The notification is only an invalidation hint; it is not itself snapshot data.

## 6. Recovery and fallback tests

Perform normal shutdown/restart only; do not force-kill unrelated processes.

1. Restart Python API while the browser is connected. Confirm EventSource reconnects, reset/reconciliation runs, and the five snapshots refresh. No fake LIVE state appears during outage.
2. Restart the BFF. Confirm the browser stream reconnects through the same origin and existing operator-session behavior remains safe.
3. Temporarily block/disable the event listener only through an approved local test configuration (not production DB changes). Confirm snapshot polling remains functional and stream status is degraded/reconnecting.
4. Logout. Confirm EventSource closes, cookie is revoked/cleared, and private LIVE state clears.
5. Confirm normal reconnect does not create duplicate snapshot writes or change SIMULATED scheduler/time.
6. Do not trigger artificial trading approval or MT5 operations for this test.

Capture sanitized status/output only. Never capture cookies, passwords, API tokens, connection strings, mentor message contents, account identifiers or private trading details.

## 7. Completion gate

Phase 3.3 can be marked COMPLETE only after:
- LISTEN delivery proven through the owner's actual Supabase Session Pooler;
- the owner-approved trigger migration is applied and catalog/security checks pass;
- normal app data writes generate expected allowlisted invalidations;
- Chrome same-origin stream and token secrecy are verified;
- API/BFF restart, cursor replay/reset, disconnect and fallback behavior pass;
- logout/session closure passes;
- no meaningful 3D rendering/performance regression appears;
- sanitized evidence is recorded in the execution log.

Until then, Phase 3.3 remains IN PROGRESS. No Phase 4 implementation starts early.

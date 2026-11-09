# Phase 5.1 — Local Workforce Command API Runbook (Windows)

**Purpose:** configure and verify the new human-supervised command surface on a disposable local database.  
**Do not use this runbook against Supabase LIVE or any production database.**  
**Secrets:** never paste values into chat, screenshots, prompts, shell transcripts, or Git; never commit local environment files.

## Topology

| Process | Address | Role |
|---|---|---|
| Python observability API | 127.0.0.1:8000 | Existing read-only sireader_api_ro |
| Python workforce command API | 127.0.0.1:8010 | New sireader_workforce_cmd plus separate command token |
| Vite frontend | 127.0.0.1:3000 | Browser origin |
| Express BFF (development) | 127.0.0.1:3001 | Operator session + CSRF; server-side command token |

The browser must only call the same-origin BFF routes. It must never call port 8010 directly.

## 1. Confirm the database is disposable and already migrated through Phase 4

Use a local PostgreSQL database created for development/QA. Confirm its host, port, database name, and owner before proceeding. The Phase 4 workforce tables/views must already exist. Do not use the Supabase project or production credentials.

From PowerShell, open an administrator psql session to that local database using the local database owner's credentials. Do not place the password in the command line.

## 2. Provision the dedicated login

Run these SQL statements as the local database owner (not as the application role):

    CREATE ROLE sireader_workforce_cmd
      LOGIN
      NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOREPLICATION NOBYPASSRLS NOINHERIT;

    GRANT CONNECT ON DATABASE LOCAL_DATABASE_NAME TO sireader_workforce_cmd;
    \password sireader_workforce_cmd

Replace LOCAL_DATABASE_NAME with the confirmed local database name. The psql password command prompts locally; type a newly generated private password twice. Do not share it. The migration checks the role attributes and refuses to run if the role is missing, inherits another role, or has elevated privileges.

If the role already exists, inspect its attributes and reset its password locally rather than creating a second role.

## 3. Apply the additive command migration locally

Still connected as the local database owner, run from the backend repository root:

    psql -h 127.0.0.1 -p 5432 -U LOCAL_DB_OWNER -d LOCAL_DATABASE_NAME -v ON_ERROR_STOP=1 -f .\migrations\2026_11_09_workforce_commands_v1.sql

Enter the local owner's password only when psql prompts. This migration adds accepting_tasks, the append-only command audit table, and scoped row-level policies. It does not create a login password.

The rollback file is migrations/2026_11_09_workforce_commands_v1_rollback.sql; use it only on this disposable database and only after stopping the command API. It intentionally leaves accepting_tasks in place because the current model/runtime uses it.

## 4. Add the private command connection and token

Generate one independent command token locally:

    python -c "import secrets; print(secrets.token_urlsafe(48))"

Copy the result directly into the two local ignored environment files. Do not paste it into chat. Use the same generated token in both locations, and make sure it is different from SIREADER_API_AUTH_TOKEN.

Backend repository .env:

    SIREADER_WORKFORCE_COMMAND_DATABASE_URL=postgresql+psycopg://sireader_workforce_cmd:URL_ENCODED_LOCAL_PASSWORD@127.0.0.1:5432/LOCAL_DATABASE_NAME
    SIREADER_WORKFORCE_COMMAND_AUTH_TOKEN=GENERATED_COMMAND_TOKEN
    WORKFORCE_COMMAND_HOST=127.0.0.1
    WORKFORCE_COMMAND_PORT=8010

URL-encode any reserved characters in the private DB password. Keep the existing DATABASE_URL and SIREADER_API_AUTH_TOKEN settings unchanged; do not reuse either for command writes.

Frontend repository .env:

    SIREADER_WORKFORCE_COMMAND_AUTH_TOKEN=THE_SAME_GENERATED_COMMAND_TOKEN

The frontend must not contain the database URL or database password. Both .env files must remain ignored by Git.

## 5. Start the command API and frontend

Open a PowerShell window in the Python backend repository root and start only the isolated command process:

    C:\SIREADER\.venv\Scripts\python.exe -m app.command_api.process

Expected bind address: 127.0.0.1:8010. Startup deliberately fails closed if the token, DB URL, role identity, exact grants, or expected RLS policies are missing.

Keep the existing read-only API on port 8000 separate. Do not start or enable a real provider/worker execution process for this acceptance test.

In another PowerShell window, open the Virtual Office frontend repository root and run:

    bun run dev

Open http://127.0.0.1:3000. The development launcher starts Vite and the BFF together; the BFF runs on 3001 and the browser talks to it through Vite's same-origin proxy.

## 6. Browser acceptance checklist

Use the existing operator login. Never share the password or session cookie.

1. Switch to LIVE and confirm the Workforce panel shows the Operator command console. Confirm worker cards show separate process status and scheduling state.
2. Create one harmless non-sensitive task such as “Phase 5 local command acceptance test”, using Haiku / Summarization. Keep worker runtime/provider execution stopped. Confirm the task appears as QUEUED and the activity/audit record is present.
3. Cancel that still-queued task. Confirm its backend status becomes CANCELLED. This tests cancellation before provider reservation without sending any provider request.
4. Pause Haiku scheduling and resume it. Confirm only accepting_tasks changes; worker enabled/process state and heartbeat are not toggled.
5. For the Retry button, optionally seed the safe FAILED fixture below, retry it once, and verify a new task is QUEUED while the original stays FAILED. Do not use a task with any provider request/reservation.
6. Switch to DEMO. Confirm the LIVE command console is absent and no command POST request is made. DEMO interactions remain simulation-only.
7. Logout. Confirm private LIVE data is cleared and the operator session is revoked.
8. In DevTools, confirm browser requests target the same-origin BFF only. The command bearer and database URL/password must not appear in the browser bundle, localStorage/sessionStorage, URLs, or responses. No direct browser request to port 8010 is permitted.

The automated CI suite separately covers same-key replay after an ambiguous timeout, same-key/different-body conflict, invalid trading-adjacent payloads, rate limits, provider-uncertain cancellation to WAITING_REVIEW/UNKNOWN, safe retry preconditions, and direct PostgreSQL RLS denial. No real provider call is needed for those checks.

## 7. Optional local retry fixture (disposable database only)

To exercise the Retry button without running a worker or provider, run the script below as the local database owner. It inserts a single generic failed Summarization task and a failed attempt with no provider request ID or reservation timestamp:

    psql -h 127.0.0.1 -p 5432 -U LOCAL_DB_OWNER -d LOCAL_DATABASE_NAME -v ON_ERROR_STOP=1 -f .\scripts\seed_phase5_retry_acceptance_fixture.sql

Use the Workforce LIVE task list to retry that fixture once. Verify the original row remains FAILED and the new row is QUEUED with the same correlation ID. Cancel the new queued task. Do not run a worker to process either row.

## 8. Shutdown and evidence

Stop the frontend launcher and the command API with Ctrl+C. Verify the ports are released. Record the observed test outcomes and any console/network errors in an owner acceptance note. Do not include passwords, token values, cookies, or full environment file contents in the report.

**Never apply the migration to Supabase LIVE as part of this acceptance.** A production migration, command credential provisioning, paid provider canary, and any trading-related operation require separate explicit review/approval.

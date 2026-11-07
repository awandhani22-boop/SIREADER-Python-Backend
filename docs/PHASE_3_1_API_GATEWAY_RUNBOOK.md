# SIREADER Phase 3.1 — API Gateway Runbook

Status: **SUPABASE DATABASE PREREQUISITES APPLIED AND EMPIRICALLY VERIFIED (2026-11-08); LOCAL API CREDENTIAL SETUP AND HTTP SMOKE TEST REMAIN.**

This runbook records the live Supabase read-only database setup and explains the remaining local-only credential/configuration steps. The database migration has already been applied to the connected project; do not re-run provisioning SQL manually.

## 1. Hard prerequisites

Before starting the live API, a DBA must have completed and recorded all of the following against the actual Supabase PostgreSQL instance:

1. The six approved views exist in schema public and have the exact DDL/projections in PHASE_3_1_API_CONTRACT.md.
2. sireader_view_owner owns all six views, is NOLOGIN, NOSUPERUSER, NOBYPASSRLS, NOINHERIT, and has only the allowlisted column-level SELECT grants on the six base tables.
3. sireader_api_ro is the login used by SIREADER_API_DATABASE_URL; it has no effective memberships that grant INHERIT or SET ROLE, no base-table/table-column privileges, no PUBLIC-derived base-table access, and SELECT only on the six views. PostgreSQL 17 records an ADMIN-only creator linkage from postgres to roles created by a CREATEROLE user (INHERIT=false, SET=false); this grants no runtime access and is explicitly excluded from the effective-membership audit.
4. All six view queries work as sireader_api_ro, and the RLS policies return the expected rows to the view owner. An empty query result alone is not proof that the intended RLS policy is correct.
5. Temporary schema CREATE and SET TRUE grants used for view ownership/verification have been removed. The remaining PostgreSQL creator ADMIN-only linkage has INHERIT=false and SET=false.
6. The actual database privilege report, RLS policy review, and six-view penetration test results are saved as deployment evidence.

The disposable PostgreSQL 16 GitHub Actions integration test is useful automated evidence, but it is not a substitute for these Supabase checks.

### 1.1 Live Supabase verification record

- Project: `otinrvvfjnyxjvzcmdia` (PostgreSQL 17.11).
- Applied migrations: `phase_3_1_read_only_observability_public_lockdown` and `phase_3_1_enable_dedicated_api_login`.
- RLS is enabled on all 15 public tables.
- `anon`, `authenticated`, and `PUBLIC` have zero table grants on public tables and the six API views.
- The six `v_api_*` views are owned by `sireader_view_owner`; it is NOLOGIN, NOINHERIT, NOBYPASSRLS, and has only the 49 allowlisted column-level SELECT grants across six base tables.
- `sireader_api_ro` has SELECT only on the six views, no base-table or base-column access, no view write access, and a default read-only transaction setting.
- Live role probe completed as `sireader_api_ro`: all six views were queried successfully in a read-only transaction; direct base-table SELECT and all view write privileges evaluated false.
- Observed row counts (metadata only): runtime 1, telemetry 46, in-flight 0, queue 0, market watches 0, audit decisions 2.
- Backend main-table privileges for `postgres` and `service_role` were retained. The six observability views have no `service_role` grants.
- Default table and sequence privileges for objects created by `postgres` in `public` were tightened so future objects are not automatically granted to `anon` or `authenticated`.

**Credential note:** `sireader_api_ro` has LOGIN enabled but no password has been set by the assistant. Set its password privately with the `psql \\password sireader_api_ro` command below before placing the URL in the local `.env`. The Supabase-managed `supabase_admin` default ACL remains platform-managed; this migration path and the SIREADER schema owner use `postgres`.

## 2. Set the dedicated database password

From PowerShell, connect using the existing database administrator credentials. The interactive `\\password` command prompts twice and does not put the new password in the SQL text/history:

```powershell
psql "host=db.otinrvvfjnyxjvzcmdia.supabase.co port=5432 dbname=postgres user=postgres sslmode=require"
```

Then, at the `psql` prompt:

```sql
\\password sireader_api_ro
\\q
```

Use a unique random password and keep it private. Do not send it in chat or commit it.

## 3. Local environment configuration

Use the existing local backend root, for example C:\SIREADER. The API reads its own settings from the root .env and does not fall back to the main DATABASE_URL.

Add the following values to the local .env only after the DBA has issued a dedicated sireader_api_ro database credential:

\`\`\`dotenv
SIREADER_API_DATABASE_URL=postgresql+psycopg://sireader_api_ro:<URL_ENCODED_PASSWORD>@db.otinrvvfjnyxjvzcmdia.supabase.co:5432/postgres?sslmode=require
SIREADER_API_AUTH_TOKEN=<LONG_RANDOM_SERVER_ONLY_TOKEN>
API_HOST=127.0.0.1
API_PORT=8000
API_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
\`\`\`

Important:
- Replace every placeholder. URL-encode reserved characters in the database password.
- Do not use the main DATABASE_URL, Supabase service_role, postgres, or any role with table ownership/superuser privileges as the API login.
- Do not commit .env, database passwords, bearer tokens, or connection strings.
- Generate a strong bearer token locally with:

  \`\`\`powershell
  python -c "import secrets; print(secrets.token_urlsafe(48))"
  \`\`\`

- Store the token only in the backend server environment. Never place it in Vite VITE_* variables, React code, JavaScript bundles, browser storage, or a public frontend request.
- Keep API_HOST=127.0.0.1. Do not override the host using Uvicorn CLI flags.

## 4. Start the standalone API

From PowerShell:

\`\`\`powershell
cd C:\SIREADER
.\.venv\Scripts\Activate.ps1
python -m app.api.server
\`\`\`

The process must start only if the live database passes the startup checks:
- authenticated current_user is sireader_api_ro;
- the connection reports transaction_read_only=on;
- API and view-owner role attributes/memberships are compliant;
- base table and column privileges are absent;
- no prohibited PUBLIC grants are present;
- the view owner has exactly the approved column-level SELECT allowlist;
- the six views have the expected owner and are queryable.

A startup failure is intentional fail-closed behavior. Do not bypass it by changing the role, turning off read-only mode, loosening the guard, or pointing to DATABASE_URL.

Stop the server with Ctrl+C. The server owns one Uvicorn worker and closes its read-only pool on graceful shutdown.

## 5. Verify read-only GET endpoints

In a second PowerShell window, enter the server-only token without echo:

\`\`\`powershell
$token = Read-Host "API bearer token"
$headers = @{ Authorization = "Bearer $token" }
$base = "http://127.0.0.1:8000"
\`\`\`

Call the five approved GET routes:

\`\`\`powershell
Invoke-RestMethod -Method Get -Uri "$base/api/v1/system/status" -Headers $headers
Invoke-RestMethod -Method Get -Uri "$base/api/v1/telemetry/usage" -Headers $headers
Invoke-RestMethod -Method Get -Uri "$base/api/v1/pipeline/active" -Headers $headers
Invoke-RestMethod -Method Get -Uri "$base/api/v1/market-watches/active" -Headers $headers
Invoke-RestMethod -Method Get -Uri "$base/api/v1/audit/decisions" -Headers $headers
\`\`\`

Expected behavior:
- Missing/wrong bearer token: 401.
- Database unavailable: sanitized 503.
- Missing runtime-control row: runtime_control = null; no invented defaults.
- An analysis record with PROCESSING: IN_FLIGHT_UNCONFIRMED, never confirmed worker BUSY.
- Unsupported billing currency: fail closed with a server error; never silently merge it into IDR or USD.
- No POST, PUT, PATCH, or DELETE business routes exist; they return 405.

## 6. Network and frontend boundary

- Local API listener: 127.0.0.1 only.
- Do not expose port 8000 directly to the public internet.
- The browser must not receive the bearer token. A future frontend integration must use a server-side BFF/proxy that stores the secret on the server and applies its own authentication and network controls.
- If a BFF runs on another host, use a private network/VPC and explicit firewall ingress rules. A reverse proxy is not automatically safe merely because the API process binds to loopback on its own machine.
- If the API becomes multi-user, multi-tenant, or internet-reachable, the accepted integer-ID risk expires; opaque public identifiers are required before release.

## 7. Test commands

The selected CI-safe test suite is:

\`\`\`powershell
pytest -q tests/api/test_config.py tests/api/test_database_lifecycle.py tests/api/test_dependencies_auth.py tests/api/test_security.py tests/api/test_endpoints.py tests/api/test_postgres_privileges.py
python -m compileall -q app/api
pytest -q test_decision_engine.py test_decision_service.py
\`\`\`

The PostgreSQL privilege integration test requires SIREADER_TEST_POSTGRES_URL to point to a disposable PostgreSQL database. In GitHub Actions, this is supplied by the ephemeral postgres:16 service container. Do not point that test variable at Supabase or a production database.

## 8. Explicitly out of scope

This API:
- does not start Telegram listeners or bots;
- does not perform AI inference;
- does not approve/reject or pause/resume trading;
- does not read/write MT5 orders;
- does not mutate any trading or runtime business state;
- does not implement SSE (deferred to Phase 3.3).

**Database status: empirically verified on the connected Supabase instance. Full local API acceptance remains pending until the user sets the private database password, configures the local `.env`, launches the process, and checks the five HTTP GET endpoints.**

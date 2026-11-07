# SIREADER Phase 3.1 — API Gateway Runbook

Status: **LOCAL RUN INSTRUCTIONS ONLY — DO NOT RUN AGAINST SUPABASE UNTIL THE EMPIRICAL VERIFICATION CHECKLIST HAS BEEN APPROVED AND COMPLETED.**

This runbook describes how to configure and start the standalone read-only API after the database prerequisites are genuinely satisfied. It does not provision roles, create views, change RLS policies, or edit the user's .env.

## 1. Hard prerequisites

Before starting the live API, a DBA must have completed and recorded all of the following against the actual Supabase PostgreSQL instance:

1. The six approved views exist in schema public and have the exact DDL/projections in PHASE_3_1_API_CONTRACT.md.
2. sireader_view_owner owns all six views, is NOLOGIN, NOSUPERUSER, NOBYPASSRLS, NOINHERIT, and has only the allowlisted column-level SELECT grants on the six base tables.
3. sireader_api_ro is the database login used by SIREADER_API_DATABASE_URL; it has no role memberships, no base-table/table-column privileges, no PUBLIC-derived base-table access, and SELECT only on the six views.
4. All six view queries work as sireader_api_ro, and the RLS policies return the expected rows to the view owner. An empty query result alone is not proof that the intended RLS policy is correct.
5. The DBA has reviewed the ownership-transfer grantor/membership result and confirmed that temporary CREATE and SET ROLE grants were removed.
6. The actual database privilege report, RLS policy review, and six-view penetration test results are saved as deployment evidence.

The disposable PostgreSQL 16 GitHub Actions integration test is useful automated evidence, but it is not a substitute for these Supabase checks.

## 2. Local environment configuration

Use the existing local backend root, for example C:\SIREADER. The API reads its own settings from the root .env and does not fall back to the main DATABASE_URL.

Add the following values to the local .env only after the DBA has issued a dedicated sireader_api_ro database credential:

\`\`\`dotenv
SIREADER_API_DATABASE_URL=postgresql+psycopg://sireader_api_ro:<URL_ENCODED_PASSWORD>@<DB_HOST>:<DB_PORT>/<DB_NAME>?sslmode=require
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

## 3. Start the standalone API

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

## 4. Verify read-only GET endpoints

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

## 5. Network and frontend boundary

- Local API listener: 127.0.0.1 only.
- Do not expose port 8000 directly to the public internet.
- The browser must not receive the bearer token. A future frontend integration must use a server-side BFF/proxy that stores the secret on the server and applies its own authentication and network controls.
- If a BFF runs on another host, use a private network/VPC and explicit firewall ingress rules. A reverse proxy is not automatically safe merely because the API process binds to loopback on its own machine.
- If the API becomes multi-user, multi-tenant, or internet-reachable, the accepted integer-ID risk expires; opaque public identifiers are required before release.

## 6. Test commands

The selected CI-safe test suite is:

\`\`\`powershell
pytest -q tests/api/test_config.py tests/api/test_database_lifecycle.py tests/api/test_dependencies_auth.py tests/api/test_security.py tests/api/test_endpoints.py tests/api/test_postgres_privileges.py
python -m compileall -q app/api
pytest -q test_decision_engine.py test_decision_service.py
\`\`\`

The PostgreSQL privilege integration test requires SIREADER_TEST_POSTGRES_URL to point to a disposable PostgreSQL database. In GitHub Actions, this is supplied by the ephemeral postgres:16 service container. Do not point that test variable at Supabase or a production database.

## 7. Explicitly out of scope

This API:
- does not start Telegram listeners or bots;
- does not perform AI inference;
- does not approve/reject or pause/resume trading;
- does not read/write MT5 orders;
- does not mutate any trading or runtime business state;
- does not implement SSE (deferred to Phase 3.3).

**Current project status remains PENDING EMPIRICAL VERIFICATION (DESIGN FREEZE - NOT IMPLEMENTATION-READY) until the actual Supabase role, view, privilege, and RLS evidence is recorded.**

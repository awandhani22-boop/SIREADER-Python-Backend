"""Dedicated command database connection and exact least-privilege verification."""
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

ALLOWED_RELATIONS = frozenset({
    "workforce_workers",
    "workforce_capabilities",
    "workforce_worker_capabilities",
    "workforce_tasks",
    "workforce_task_attempts",
    "workforce_activity_events",
    "workforce_command_audit",
})

EXPECTED_TABLE_PRIVILEGES = {
    "workforce_workers": {"SELECT"},
    "workforce_capabilities": {"SELECT"},
    "workforce_worker_capabilities": {"SELECT"},
    "workforce_tasks": {"SELECT", "INSERT"},
    "workforce_task_attempts": {"SELECT"},
    "workforce_activity_events": {"INSERT"},
    "workforce_command_audit": {"SELECT", "INSERT"},
}
EXPECTED_COLUMN_UPDATES = {
    "workforce_workers": {"accepting_tasks", "updated_at"},
    "workforce_tasks": {
        "status", "updated_at", "payload_expires_at",
        "lease_token", "lease_owner", "lease_expires_at",
    },
    "workforce_task_attempts": {"status", "error_code", "finished_at"},
}


def create_command_engine(database_url: str) -> Engine:
    return create_engine(
        database_url, pool_pre_ping=True, pool_size=2, max_overflow=0,
        pool_timeout=5, connect_args={"connect_timeout": 5},
    )


def create_command_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def validate_command_database_security(session: Session) -> None:
    """Fail closed unless this login has only the declared workforce privileges."""
    identity = session.execute(text("""
        SELECT current_user AS role_name,
          current_setting('transaction_read_only') AS transaction_read_only,
          r.rolcanlogin, r.rolsuper, r.rolinherit, r.rolcreatedb,
          r.rolcreaterole, r.rolreplication, r.rolbypassrls,
          has_schema_privilege(current_user, 'public', 'USAGE') AS schema_usage,
          has_schema_privilege(current_user, 'public', 'CREATE') AS schema_create,
          (SELECT count(*) FROM pg_catalog.pg_auth_members m WHERE m.member = r.oid) AS membership_count
        FROM pg_catalog.pg_roles r WHERE r.rolname = current_user
    """)).mappings().one_or_none()
    if identity is None:
        raise RuntimeError("Unable to verify workforce command database identity.")
    safe_identity = (
        identity["role_name"] == "sireader_workforce_cmd"
        and identity["transaction_read_only"] == "off"
        and identity["rolcanlogin"] is True
        and identity["rolsuper"] is False
        and identity["rolinherit"] is False
        and identity["rolcreatedb"] is False
        and identity["rolcreaterole"] is False
        and identity["rolreplication"] is False
        and identity["rolbypassrls"] is False
        and identity["schema_usage"] is True
        and identity["schema_create"] is False
        and int(identity["membership_count"]) == 0
    )
    if not safe_identity:
        raise RuntimeError("Workforce command database role violates the least-privilege identity contract.")

    relations = session.execute(text("""
        SELECT c.oid, c.relname, c.relkind,
          has_table_privilege(current_user, c.oid, 'SELECT') AS can_select,
          has_table_privilege(current_user, c.oid, 'INSERT') AS can_insert,
          has_table_privilege(current_user, c.oid, 'UPDATE') AS can_update,
          has_table_privilege(current_user, c.oid, 'DELETE') AS can_delete,
          has_table_privilege(current_user, c.oid, 'TRUNCATE') AS can_truncate,
          has_table_privilege(current_user, c.oid, 'REFERENCES') AS can_reference,
          has_table_privilege(current_user, c.oid, 'TRIGGER') AS can_trigger
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m','f')
    """)).mappings().all()


    protected_tables = {
        "workforce_workers", "workforce_capabilities", "workforce_worker_capabilities",
        "workforce_tasks", "workforce_task_attempts", "workforce_activity_events",
        "workforce_command_audit",
    }
    rls_rows = session.execute(text("""
        SELECT c.relname, c.relrowsecurity
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = ANY(:table_names)
    """), {"table_names": sorted(protected_tables)}).mappings().all()
    if (
        {row["relname"] for row in rls_rows} != protected_tables
        or any(not row["relrowsecurity"] for row in rls_rows)
    ):
        raise RuntimeError("Row-level security must be enabled on every command-accessible workforce table.")

    policies = session.execute(text("""
        SELECT tablename, policyname, roles, cmd, qual, with_check
        FROM pg_catalog.pg_policies
        WHERE schemaname = 'public'
    """)).mappings().all()
    expected_policies = {
        ("workforce_workers", "workforce_cmd_workers_select", "SELECT"),
        ("workforce_workers", "workforce_cmd_workers_update", "UPDATE"),
        ("workforce_capabilities", "workforce_cmd_capabilities_select", "SELECT"),
        ("workforce_worker_capabilities", "workforce_cmd_worker_capabilities_select", "SELECT"),
        ("workforce_tasks", "workforce_cmd_tasks_select", "SELECT"),
        ("workforce_tasks", "workforce_cmd_tasks_insert", "INSERT"),
        ("workforce_tasks", "workforce_cmd_tasks_update", "UPDATE"),
        ("workforce_task_attempts", "workforce_cmd_attempts_select", "SELECT"),
        ("workforce_task_attempts", "workforce_cmd_attempts_update", "UPDATE"),
        ("workforce_activity_events", "workforce_cmd_activity_insert", "INSERT"),
        ("workforce_command_audit", "workforce_cmd_audit_select", "SELECT"),
        ("workforce_command_audit", "workforce_cmd_audit_insert", "INSERT"),
    }
    found = set()
    for policy in policies:
        roles = {str(role).lower() for role in policy["roles"]}
        if policy["tablename"] in protected_tables and "public" in roles:
            raise RuntimeError("PUBLIC must not have workforce row-level policies.")
        if "sireader_workforce_cmd" not in roles:
            continue
        identity_key = (policy["tablename"], policy["policyname"], policy["cmd"])
        found.add(identity_key)
        expression = policy["qual"] or policy["with_check"] or ""
        normalized = str(expression).replace("(", "").replace(")", "").strip().lower()
        if normalized == "true":
            raise RuntimeError("Workforce command RLS policy must not grant unrestricted rows.")
    if found != expected_policies:
        raise RuntimeError("Workforce command RLS policy set does not match the non-trading allowlist boundary.")

    sequence_access = session.execute(text("""
        SELECT EXISTS (
          SELECT 1 FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
          WHERE n.nspname = 'public' AND c.relkind = 'S'
            AND (
              has_sequence_privilege(current_user, c.oid, 'USAGE')
              OR has_sequence_privilege(current_user, c.oid, 'SELECT')
              OR has_sequence_privilege(current_user, c.oid, 'UPDATE')
            )
        )
    """)).scalar_one()
    if sequence_access:
        raise RuntimeError("Workforce command role must not access public sequences.")

    for relation in relations:
        name = relation["relname"]
        actual = {
            privilege for privilege, key in (
                ("SELECT", "can_select"), ("INSERT", "can_insert"),
                ("UPDATE", "can_update"), ("DELETE", "can_delete"),
                ("TRUNCATE", "can_truncate"), ("REFERENCES", "can_reference"),
                ("TRIGGER", "can_trigger"),
            ) if relation[key]
        }
        if name not in ALLOWED_RELATIONS:
            if actual:
                raise RuntimeError("Workforce command role has access to a non-workforce database relation.")
            continue
        if actual != EXPECTED_TABLE_PRIVILEGES[name]:
            raise RuntimeError("Workforce command role has an unexpected table-level privilege.")

    # Column-level grants do not appear in has_table_privilege(). Audit all
    # effective UPDATE/INSERT/REFERENCES rights so a stray column grant cannot
    # silently broaden the command role's reach.
    columns = session.execute(text("""
        SELECT c.relname, a.attname,
          has_column_privilege(current_user, c.oid, a.attnum, 'SELECT') AS can_select,
          has_column_privilege(current_user, c.oid, a.attnum, 'INSERT') AS can_insert,
          has_column_privilege(current_user, c.oid, a.attnum, 'UPDATE') AS can_update,
          has_column_privilege(current_user, c.oid, a.attnum, 'REFERENCES') AS can_reference
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
        WHERE n.nspname = 'public' AND c.relkind IN ('r','p')
          AND a.attnum > 0 AND NOT a.attisdropped
    """)).mappings().all()
    for column in columns:
        name = column["relname"]
        if name not in ALLOWED_RELATIONS:
            if any(column[key] for key in ("can_select", "can_insert", "can_update", "can_reference")):
                raise RuntimeError("Workforce command role has a column grant on a non-workforce relation.")
            continue
        # Table-wide SELECT/INSERT are expected on a subset of tables. UPDATE
        # is deliberately column-scoped; all other column UPDATEs are rejected.
        if column["can_select"] and "SELECT" not in EXPECTED_TABLE_PRIVILEGES[name]:
            raise RuntimeError("Workforce command role has an unapproved column-level SELECT grant.")
        if column["can_insert"] and "INSERT" not in EXPECTED_TABLE_PRIVILEGES[name]:
            raise RuntimeError("Workforce command role has an unapproved column-level INSERT grant.")
        if column["can_update"] and column["attname"] not in EXPECTED_COLUMN_UPDATES.get(name, set()):
            raise RuntimeError("Workforce command role can update an unapproved workforce column.")
        if column["can_reference"]:
            raise RuntimeError("Workforce command role must not hold REFERENCES on workforce columns.")

    for relation, expected_columns in EXPECTED_COLUMN_UPDATES.items():
        actual_columns = {
            row["attname"] for row in columns
            if row["relname"] == relation and row["can_update"]
        }
        if actual_columns != expected_columns:
            raise RuntimeError("Workforce command role column-level UPDATE grants do not match the allowlist.")

    audit_mutation = session.execute(text("""
        SELECT (
          has_table_privilege(current_user, 'public.workforce_command_audit', 'UPDATE')
          OR has_table_privilege(current_user, 'public.workforce_command_audit', 'DELETE')
          OR has_table_privilege(current_user, 'public.workforce_command_audit', 'TRUNCATE')
        )
    """)).scalar_one()
    if audit_mutation:
        raise RuntimeError("Workforce command audit must be append-only for the command role.")

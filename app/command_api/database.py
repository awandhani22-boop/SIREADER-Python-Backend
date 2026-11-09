"""Dedicated write connection and privilege verification for Phase 5.1."""
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

ALLOWED_RELATIONS = {
    "workforce_workers", "workforce_capabilities", "workforce_worker_capabilities",
    "workforce_tasks", "workforce_task_attempts", "workforce_activity_events",
    "workforce_command_audit",
}

def create_command_engine(database_url: str) -> Engine:
    return create_engine(
        database_url, pool_pre_ping=True, pool_size=2, max_overflow=0,
        pool_timeout=5, connect_args={"connect_timeout": 5},
    )

def create_command_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

def validate_command_database_security(session: Session) -> None:
    """Fail closed unless this is the dedicated, narrowly granted login."""
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
    safe = (
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
    if not safe:
        raise RuntimeError("Workforce command database role violates the least-privilege identity contract.")

    rows = session.execute(text("""
        SELECT c.relname AS relation_name
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m','f')
          AND (
            has_table_privilege(current_user, c.oid, 'SELECT')
            OR has_table_privilege(current_user, c.oid, 'INSERT')
            OR has_table_privilege(current_user, c.oid, 'UPDATE')
            OR has_table_privilege(current_user, c.oid, 'DELETE')
            OR has_table_privilege(current_user, c.oid, 'TRUNCATE')
            OR has_table_privilege(current_user, c.oid, 'REFERENCES')
            OR has_table_privilege(current_user, c.oid, 'TRIGGER')
          )
    """)).mappings().all()
    unexpected = {row["relation_name"] for row in rows} - ALLOWED_RELATIONS
    if unexpected:
        raise RuntimeError("Workforce command role has access to non-workforce database relations.")

    required_select = {
        "workforce_workers", "workforce_capabilities", "workforce_worker_capabilities",
        "workforce_tasks", "workforce_task_attempts", "workforce_command_audit",
    }
    required_insert = {
        "workforce_tasks", "workforce_activity_events", "workforce_command_audit",
    }
    required_update_columns = {
        "workforce_workers": ("accepting_tasks", "updated_at"),
        "workforce_tasks": (
            "status", "updated_at", "payload_expires_at", "lease_token", "lease_owner", "lease_expires_at",
        ),
        "workforce_task_attempts": ("status", "error_code", "finished_at"),
    }
    for relation in required_select:
        granted = session.execute(
            text("SELECT has_table_privilege(current_user, :relation, 'SELECT')"),
            {"relation": "public." + relation},
        ).scalar_one()
        if not granted:
            raise RuntimeError("Workforce command role is missing a required SELECT grant.")
    for relation in required_insert:
        granted = session.execute(
            text("SELECT has_table_privilege(current_user, :relation, 'INSERT')"),
            {"relation": "public." + relation},
        ).scalar_one()
        if not granted:
            raise RuntimeError("Workforce command role is missing a required INSERT grant.")
    for relation, columns in required_update_columns.items():
        for column in columns:
            granted = session.execute(
                text("SELECT has_column_privilege(current_user, :relation, :column, 'UPDATE')"),
                {"relation": "public." + relation, "column": column},
            ).scalar_one()
            if not granted:
                raise RuntimeError("Workforce command role is missing a required column-level UPDATE grant.")

    audit_mutation = session.execute(text("""
        SELECT (
          has_table_privilege(current_user, 'public.workforce_command_audit', 'UPDATE')
          OR has_table_privilege(current_user, 'public.workforce_command_audit', 'DELETE')
          OR has_table_privilege(current_user, 'public.workforce_command_audit', 'TRUNCATE')
        )
    """)).scalar_one()
    if audit_mutation:
        raise RuntimeError("Workforce command audit must be append-only for the command role.")

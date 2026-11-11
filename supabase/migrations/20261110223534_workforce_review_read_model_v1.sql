-- SIREADER production migration: Human Review read model and quarantine storage.
-- This migration intentionally does not create workforce_command_audit, a command
-- login, a worker login, or any command/worker grants. Writes remain unavailable
-- until a separately reviewed production writer boundary is deployed.

BEGIN;

DO $guard$
BEGIN
    IF to_regclass('public.workforce_tasks') IS NULL
       OR to_regclass('public.workforce_task_attempts') IS NULL
       OR to_regclass('public.workforce_task_reviews') IS NOT NULL
       OR to_regclass('public.workforce_task_review_payloads') IS NOT NULL THEN
        RAISE EXCEPTION 'Workforce base schema is missing or review objects already exist; reconcile before applying';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sireader_view_owner')
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sireader_api_ro') THEN
        RAISE EXCEPTION 'Required Phase 3.1 SIREADER roles are missing';
    END IF;
    IF has_schema_privilege('sireader_view_owner', 'public', 'CREATE') THEN
        RAISE EXCEPTION 'sireader_view_owner unexpectedly has CREATE on public before the migration';
    END IF;
END;
$guard$;

CREATE TABLE public.workforce_task_reviews (
    review_id varchar(64) PRIMARY KEY,
    task_id varchar(64) NOT NULL
        REFERENCES public.workforce_tasks(task_id) ON DELETE RESTRICT,
    attempt_id varchar(64) NOT NULL
        REFERENCES public.workforce_task_attempts(attempt_id) ON DELETE RESTRICT,
    status varchar(24) NOT NULL DEFAULT 'PENDING',
    original_output_sha256 char(64) NOT NULL,
    adjusted_output_sha256 char(64),
    adjudicated_by varchar(100),
    adjudicated_at timestamptz,
    reason_code varchar(48),
    notes varchar(500),
    review_expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uq_workforce_task_reviews_task UNIQUE (task_id),
    CONSTRAINT ck_workforce_task_reviews_status
        CHECK (status IN ('PENDING','APPROVED','EDITED','REJECTED','EXPIRED')),
    CONSTRAINT ck_workforce_task_reviews_sha256_format
        CHECK (
            original_output_sha256 ~ '^[0-9a-f]{64}$'
            AND (adjusted_output_sha256 IS NULL
                 OR adjusted_output_sha256 ~ '^[0-9a-f]{64}$')
        ),
    CONSTRAINT ck_workforce_task_reviews_adjudication
        CHECK (
            (status = 'PENDING' AND adjudicated_at IS NULL AND adjudicated_by IS NULL)
            OR (status = 'EXPIRED' AND adjudicated_at IS NOT NULL
                AND adjudicated_by = 'system' AND reason_code = 'REVIEW_TIMED_OUT')
            OR (status IN ('APPROVED','EDITED','REJECTED')
                AND adjudicated_at IS NOT NULL AND adjudicated_by IS NOT NULL)
        ),
    CONSTRAINT ck_workforce_task_reviews_edited
        CHECK (
            (status = 'EDITED' AND adjusted_output_sha256 IS NOT NULL)
            OR (status <> 'EDITED' AND adjusted_output_sha256 IS NULL)
        ),
    CONSTRAINT ck_workforce_task_reviews_rejected
        CHECK (
            (status = 'REJECTED' AND reason_code IS NOT NULL)
            OR (status <> 'REJECTED'
                AND (status <> 'EXPIRED' OR reason_code = 'REVIEW_TIMED_OUT'))
        )
);

CREATE INDEX ix_workforce_task_reviews_status_created
    ON public.workforce_task_reviews(status, created_at DESC);
CREATE INDEX ix_workforce_task_reviews_expires
    ON public.workforce_task_reviews(review_expires_at)
    WHERE status = 'PENDING';

CREATE TABLE public.workforce_task_review_payloads (
    review_id varchar(64) PRIMARY KEY
        REFERENCES public.workforce_task_reviews(review_id) ON DELETE RESTRICT,
    original_output jsonb,
    adjusted_output jsonb,
    payload_purged_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_workforce_review_payloads_size
        CHECK (
            (original_output IS NULL OR pg_column_size(original_output) <= 65536)
            AND (adjusted_output IS NULL OR pg_column_size(adjusted_output) <= 65536)
        ),
    CONSTRAINT ck_workforce_review_payloads_purged
        CHECK (
            (payload_purged_at IS NULL AND original_output IS NOT NULL)
            OR (
                payload_purged_at IS NOT NULL
                AND original_output IS NULL
                AND adjusted_output IS NULL
            )
        )
);

-- Metadata starts as PENDING and becomes immutable after a terminal decision.
CREATE FUNCTION public.sireader_guard_review_metadata_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'workforce_task_reviews records are permanent and cannot be deleted';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF NEW.status IS DISTINCT FROM 'PENDING'
           OR NEW.adjusted_output_sha256 IS NOT NULL
           OR NEW.adjudicated_by IS NOT NULL
           OR NEW.adjudicated_at IS NOT NULL
           OR NEW.reason_code IS NOT NULL
           OR NEW.notes IS NOT NULL THEN
            RAISE EXCEPTION 'New review metadata must start in PENDING without adjudication fields';
        END IF;
        NEW.updated_at = clock_timestamp();
        RETURN NEW;
    END IF;

    IF OLD.original_output_sha256 IS DISTINCT FROM NEW.original_output_sha256
       OR OLD.task_id IS DISTINCT FROM NEW.task_id
       OR OLD.attempt_id IS DISTINCT FROM NEW.attempt_id THEN
        RAISE EXCEPTION 'Core governance references and original hash are immutable';
    END IF;

    IF OLD.status IN ('APPROVED','EDITED','REJECTED','EXPIRED') THEN
        RAISE EXCEPTION 'Terminal review state % cannot be modified', OLD.status;
    END IF;

    IF OLD.status <> 'PENDING'
       OR NEW.status NOT IN ('APPROVED','EDITED','REJECTED','EXPIRED') THEN
        RAISE EXCEPTION 'Only a PENDING review can transition to a terminal status';
    END IF;

    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END;
$function$;

CREATE TRIGGER trg_workforce_task_reviews_guard
BEFORE INSERT OR UPDATE OR DELETE ON public.workforce_task_reviews
FOR EACH ROW EXECUTE FUNCTION public.sireader_guard_review_metadata_v1();

-- Original output is immutable; adjusted output is write-once after EDITED;
-- purge requires terminal review plus parent task retention expiry.
CREATE FUNCTION public.sireader_guard_review_payloads_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $function$
DECLARE
    review_status varchar(24);
    task_expires_at timestamptz;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'workforce_task_review_payloads records cannot be deleted; use retention purge';
    END IF;

    IF TG_OP = 'INSERT' THEN
        SELECT status INTO review_status
        FROM public.workforce_task_reviews
        WHERE review_id = NEW.review_id;

        IF review_status IS DISTINCT FROM 'PENDING'
           OR NEW.original_output IS NULL
           OR NEW.adjusted_output IS NOT NULL
           OR NEW.payload_purged_at IS NOT NULL THEN
            RAISE EXCEPTION 'Initial review payload must contain only original_output for a PENDING review';
        END IF;
        NEW.updated_at = clock_timestamp();
        RETURN NEW;
    END IF;

    IF OLD.payload_purged_at IS NOT NULL THEN
        RAISE EXCEPTION 'Purged review payload cannot be modified or restored';
    END IF;

    IF NEW.payload_purged_at IS NOT NULL THEN
        IF NEW.original_output IS NOT NULL OR NEW.adjusted_output IS NOT NULL THEN
            RAISE EXCEPTION 'Purging requires original_output and adjusted_output to be NULL';
        END IF;

        SELECT r.status, t.payload_expires_at
          INTO review_status, task_expires_at
        FROM public.workforce_task_reviews AS r
        JOIN public.workforce_tasks AS t ON t.task_id = r.task_id
        WHERE r.review_id = OLD.review_id;

        IF review_status NOT IN ('APPROVED','EDITED','REJECTED','EXPIRED') THEN
            RAISE EXCEPTION 'Cannot purge an active review';
        END IF;
        IF task_expires_at IS NULL OR task_expires_at > clock_timestamp() THEN
            RAISE EXCEPTION 'Cannot purge before parent task retention expiry';
        END IF;

        NEW.payload_purged_at = clock_timestamp();
    ELSE
        IF OLD.original_output IS NOT NULL
           AND NEW.original_output IS DISTINCT FROM OLD.original_output THEN
            RAISE EXCEPTION 'Original review output is immutable';
        END IF;

        IF OLD.adjusted_output IS NOT NULL
           AND NEW.adjusted_output IS DISTINCT FROM OLD.adjusted_output THEN
            RAISE EXCEPTION 'Adjusted review output is write-once';
        END IF;

        IF OLD.adjusted_output IS NULL AND NEW.adjusted_output IS NOT NULL THEN
            SELECT status INTO review_status
            FROM public.workforce_task_reviews
            WHERE review_id = OLD.review_id;

            IF review_status IS DISTINCT FROM 'EDITED' THEN
                RAISE EXCEPTION 'Adjusted output can only be written for an EDITED review';
            END IF;
        END IF;
    END IF;

    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END;
$function$;

CREATE TRIGGER trg_workforce_task_review_payloads_guard
BEFORE INSERT OR UPDATE OR DELETE ON public.workforce_task_review_payloads
FOR EACH ROW EXECUTE FUNCTION public.sireader_guard_review_payloads_v1();

-- Trigger functions are not callable directly by client or API roles.
REVOKE ALL ON FUNCTION public.sireader_guard_review_metadata_v1()
    FROM PUBLIC, anon, authenticated, service_role, sireader_api_ro;
REVOKE ALL ON FUNCTION public.sireader_guard_review_payloads_v1()
    FROM PUBLIC, anon, authenticated, service_role, sireader_api_ro;

-- RLS grants read access only to the NOLOGIN view owner. No production writer
-- or command identity receives any permission in this read-model migration.
ALTER TABLE public.workforce_task_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.workforce_task_review_payloads ENABLE ROW LEVEL SECURITY;

CREATE POLICY workforce_view_owner_select
    ON public.workforce_task_reviews FOR SELECT
    TO sireader_view_owner USING (true);
CREATE POLICY workforce_view_owner_select
    ON public.workforce_task_review_payloads FOR SELECT
    TO sireader_view_owner USING (true);

REVOKE ALL ON TABLE
    public.workforce_task_reviews,
    public.workforce_task_review_payloads
FROM PUBLIC, anon, authenticated, service_role, sireader_api_ro;

GRANT SELECT (
    review_id, task_id, attempt_id, status, original_output_sha256,
    adjusted_output_sha256, adjudicated_by, adjudicated_at, reason_code,
    notes, review_expires_at, created_at, updated_at
) ON public.workforce_task_reviews TO sireader_view_owner;

GRANT SELECT (
    review_id, original_output, adjusted_output, payload_purged_at
) ON public.workforce_task_review_payloads TO sireader_view_owner;

CREATE VIEW public.v_api_workforce_task_reviews
WITH (security_barrier = true) AS
SELECT
    r.review_id,
    r.task_id,
    r.attempt_id,
    r.status,
    r.original_output_sha256,
    r.adjusted_output_sha256,
    r.adjudicated_by,
    r.adjudicated_at,
    r.reason_code,
    r.notes,
    r.review_expires_at,
    r.created_at,
    r.updated_at,
    p.original_output,
    p.adjusted_output,
    p.payload_purged_at
FROM public.workforce_task_reviews AS r
LEFT JOIN public.workforce_task_review_payloads AS p
  ON p.review_id = r.review_id;

GRANT CREATE ON SCHEMA public TO sireader_view_owner;
ALTER VIEW public.v_api_workforce_task_reviews OWNER TO sireader_view_owner;
REVOKE CREATE ON SCHEMA public FROM sireader_view_owner;

REVOKE ALL ON public.v_api_workforce_task_reviews
    FROM PUBLIC, anon, authenticated, service_role, sireader_api_ro;
GRANT SELECT ON public.v_api_workforce_task_reviews TO sireader_api_ro;

COMMIT;

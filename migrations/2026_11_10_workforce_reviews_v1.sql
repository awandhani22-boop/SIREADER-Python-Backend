-- SIREADER Phase 5.2.2 — Human Output Review, Quality Adjudication & Governance.
-- Apply only after provisioning dedicated sireader_workforce_cmd, sireader_api_ro, sireader_view_owner roles.
-- This migration is strictly for local/staging disposable databases. Never apply to Supabase LIVE.
BEGIN;

DO $guard$
DECLARE
    cmd_role record;
BEGIN
    SELECT * INTO cmd_role FROM pg_catalog.pg_roles
    WHERE rolname = 'sireader_workforce_cmd';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Pre-provision dedicated login role sireader_workforce_cmd before this migration';
    END IF;
    IF NOT cmd_role.rolcanlogin OR cmd_role.rolsuper OR cmd_role.rolcreatedb
       OR cmd_role.rolcreaterole OR cmd_role.rolreplication OR cmd_role.rolbypassrls
       OR cmd_role.rolinherit THEN
        RAISE EXCEPTION 'sireader_workforce_cmd role attributes violate least privilege';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sireader_view_owner')
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sireader_api_ro') THEN
        RAISE EXCEPTION 'Required Phase 4 view owner/API roles are missing';
    END IF;
END;
$guard$;

-- 1. Governance Metadata Table (Permanent, Immutable once terminal)
CREATE TABLE IF NOT EXISTS public.workforce_task_reviews (
    review_id varchar(64) PRIMARY KEY,
    task_id varchar(64) NOT NULL REFERENCES public.workforce_tasks(task_id) ON DELETE RESTRICT,
    attempt_id varchar(64) NOT NULL REFERENCES public.workforce_task_attempts(attempt_id) ON DELETE RESTRICT,
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
    CONSTRAINT ck_workforce_task_reviews_status CHECK (
        status IN ('PENDING', 'APPROVED', 'EDITED', 'REJECTED', 'EXPIRED')
    ),
    CONSTRAINT ck_workforce_task_reviews_sha256_format CHECK (
        original_output_sha256 ~ '^[0-9a-f]{64}$' AND
        (adjusted_output_sha256 IS NULL OR adjusted_output_sha256 ~ '^[0-9a-f]{64}$')
    ),
    CONSTRAINT ck_workforce_task_reviews_adjudication CHECK (
        (status = 'PENDING' AND adjudicated_at IS NULL AND adjudicated_by IS NULL) OR
        (status = 'EXPIRED' AND adjudicated_at IS NOT NULL AND adjudicated_by = 'system' AND reason_code = 'REVIEW_TIMED_OUT') OR
        (status IN ('APPROVED', 'EDITED', 'REJECTED') AND adjudicated_at IS NOT NULL AND adjudicated_by IS NOT NULL)
    ),
    CONSTRAINT ck_workforce_task_reviews_edited CHECK (
        (status = 'EDITED' AND adjusted_output_sha256 IS NOT NULL) OR
        (status != 'EDITED' AND adjusted_output_sha256 IS NULL)
    ),
    CONSTRAINT ck_workforce_task_reviews_rejected CHECK (
        (status = 'REJECTED' AND reason_code IS NOT NULL) OR
        (status != 'REJECTED' AND (status != 'EXPIRED' OR reason_code = 'REVIEW_TIMED_OUT'))
    )
);

CREATE INDEX IF NOT EXISTS ix_workforce_task_reviews_status_created 
    ON public.workforce_task_reviews (status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_workforce_task_reviews_expires 
    ON public.workforce_task_reviews (review_expires_at) WHERE status = 'PENDING';

-- 2. Review Payloads Table (Subject to retention purging, 64 KiB cap)
CREATE TABLE IF NOT EXISTS public.workforce_task_review_payloads (
    review_id varchar(64) PRIMARY KEY REFERENCES public.workforce_task_reviews(review_id) ON DELETE RESTRICT,
    original_output jsonb,
    adjusted_output jsonb,
    payload_purged_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_workforce_review_payloads_size CHECK (
        (original_output IS NULL OR pg_column_size(original_output) <= 65536) AND
        (adjusted_output IS NULL OR pg_column_size(adjusted_output) <= 65536)
    ),
    CONSTRAINT ck_workforce_review_payloads_purged CHECK (
        (payload_purged_at IS NULL AND original_output IS NOT NULL) OR
        (payload_purged_at IS NOT NULL AND original_output IS NULL AND adjusted_output IS NULL)
    )
);

-- 3. Triggers for Metadata Immutability & Rejection of Delete
CREATE OR REPLACE FUNCTION public.sireader_guard_review_metadata_v1()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'workforce_task_reviews records are permanent and cannot be deleted';
    END IF;

    -- Core identity references and original hash are strictly immutable
    IF OLD.original_output_sha256 IS DISTINCT FROM NEW.original_output_sha256 OR
       OLD.task_id IS DISTINCT FROM NEW.task_id OR
       OLD.attempt_id IS DISTINCT FROM NEW.attempt_id THEN
        RAISE EXCEPTION 'Core governance references and original hash are immutable.';
    END IF;

    -- Reject modifications once review is terminal
    IF OLD.status IN ('APPROVED', 'EDITED', 'REJECTED', 'EXPIRED') THEN
        RAISE EXCEPTION 'Terminal review state % cannot be modified.', OLD.status;
    END IF;

    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_workforce_task_reviews_guard ON public.workforce_task_reviews;
CREATE TRIGGER trg_workforce_task_reviews_guard
BEFORE UPDATE OR DELETE ON public.workforce_task_reviews
FOR EACH ROW EXECUTE FUNCTION public.sireader_guard_review_metadata_v1();

-- 4. Triggers for Payload Storage Immutability & Anti-Resurrection
CREATE OR REPLACE FUNCTION public.sireader_guard_review_payloads_v1()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
    r_status varchar(24);
    t_expires_at timestamptz;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'workforce_task_review_payloads records cannot be deleted; use retention purge';
    END IF;

    -- Anti-resurrection: once purged, cannot modify or restore
    IF OLD.payload_purged_at IS NOT NULL THEN
        RAISE EXCEPTION 'Purged review payload cannot be modified or restored.';
    END IF;

    -- Purge operation validation
    IF NEW.payload_purged_at IS NOT NULL THEN
        IF NEW.original_output IS NOT NULL OR NEW.adjusted_output IS NOT NULL THEN
            RAISE EXCEPTION 'Purging requires original_output and adjusted_output to be NULL.';
        END IF;

        SELECT r.status, t.payload_expires_at INTO r_status, t_expires_at
        FROM public.workforce_task_reviews r
        JOIN public.workforce_tasks t ON t.task_id = r.task_id
        WHERE r.review_id = OLD.review_id;

        IF r_status NOT IN ('APPROVED', 'EDITED', 'REJECTED', 'EXPIRED') THEN
            RAISE EXCEPTION 'Cannot purge payloads for active review % in status %.', OLD.review_id, r_status;
        END IF;

        IF t_expires_at IS NULL OR t_expires_at > clock_timestamp() THEN
            RAISE EXCEPTION 'Cannot purge review payload before parent task expiration (expires at: %).', t_expires_at;
        END IF;

        -- Database independently timestamps the purge; callers cannot pass arbitrary timestamp
        NEW.payload_purged_at = clock_timestamp();
    ELSE
        -- Not purging: original_output is strictly immutable
        IF OLD.original_output IS NOT NULL AND NEW.original_output IS DISTINCT FROM OLD.original_output THEN
            RAISE EXCEPTION 'Original review output is immutable.';
        END IF;

        -- adjusted_output is write-once and can ONLY be introduced when review is in EDITED status
        IF OLD.adjusted_output IS NOT NULL AND NEW.adjusted_output IS DISTINCT FROM OLD.adjusted_output THEN
            RAISE EXCEPTION 'Adjusted review output is write-once.';
        END IF;

        IF OLD.adjusted_output IS NULL AND NEW.adjusted_output IS NOT NULL THEN
            SELECT status INTO r_status FROM public.workforce_task_reviews WHERE review_id = OLD.review_id;
            IF r_status IS DISTINCT FROM 'EDITED' THEN
                RAISE EXCEPTION 'Adjusted review output can only be written for reviews in EDITED status (current status: %)', r_status;
            END IF;
        END IF;
    END IF;

    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_workforce_task_review_payloads_guard ON public.workforce_task_review_payloads;
CREATE TRIGGER trg_workforce_task_review_payloads_guard
BEFORE UPDATE OR DELETE ON public.workforce_task_review_payloads
FOR EACH ROW EXECUTE FUNCTION public.sireader_guard_review_payloads_v1();

-- 5a. Additive update to command audit constraint
ALTER TABLE public.workforce_command_audit
    DROP CONSTRAINT IF EXISTS workforce_command_audit_command_type_check;

ALTER TABLE public.workforce_command_audit
    ADD CONSTRAINT workforce_command_audit_command_type_check CHECK (
        command_type::text = ANY (ARRAY['TASK_ENQUEUE'::character varying, 'TASK_CANCEL'::character varying, 'TASK_RETRY'::character varying, 'WORKER_SCHEDULING_SET'::character varying, 'REVIEW_ADJUDICATE'::character varying]::text[])
    );

-- 5b. Additive update to activity events constraint
ALTER TABLE public.workforce_activity_events
    DROP CONSTRAINT IF EXISTS workforce_activity_events_event_type_check;

ALTER TABLE public.workforce_activity_events
    ADD CONSTRAINT workforce_activity_events_event_type_check CHECK (
        event_type::text = ANY (ARRAY[
            'TASK_ENQUEUED'::character varying, 'TASK_CLAIMED'::character varying,
            'TASK_STARTED'::character varying, 'PROVIDER_REQUESTED'::character varying,
            'TASK_RETRIED'::character varying, 'TASK_COMPLETED'::character varying,
            'TASK_FAILED'::character varying, 'TASK_CANCELLED'::character varying,
            'TASK_WAITING_REVIEW'::character varying, 'TASK_DEAD_LETTERED'::character varying,
            'WORKER_HEARTBEAT'::character varying, 'WORKER_STATE_CHANGED'::character varying,
            'HUMAN_REVIEW_QUEUED'::character varying, 'HUMAN_REVIEW_APPROVED'::character varying,
            'HUMAN_REVIEW_EDITED'::character varying, 'HUMAN_REVIEW_REJECTED'::character varying,
            'HUMAN_REVIEW_EXPIRED'::character varying
        ]::text[])
    );

-- 6. Security-Barrier Read View
CREATE OR REPLACE VIEW public.v_api_workforce_task_reviews
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
FROM public.workforce_task_reviews r
LEFT JOIN public.workforce_task_review_payloads p ON p.review_id = r.review_id;

ALTER VIEW public.v_api_workforce_task_reviews OWNER TO sireader_view_owner;

-- 7. Role Privileges & Access Boundaries
REVOKE ALL ON public.workforce_task_reviews FROM public, sireader_api_ro;
REVOKE ALL ON public.workforce_task_review_payloads FROM public, sireader_api_ro;

GRANT SELECT ON public.v_api_workforce_task_reviews TO sireader_api_ro;

-- Command role: least privilege for review adjudication
GRANT SELECT, INSERT ON public.workforce_task_reviews TO sireader_workforce_cmd;
GRANT UPDATE (status, adjusted_output_sha256, adjudicated_by, adjudicated_at, reason_code, notes, updated_at)
    ON public.workforce_task_reviews TO sireader_workforce_cmd;

GRANT SELECT, INSERT ON public.workforce_task_review_payloads TO sireader_workforce_cmd;
GRANT UPDATE (adjusted_output, updated_at)
    ON public.workforce_task_review_payloads TO sireader_workforce_cmd;

COMMIT;

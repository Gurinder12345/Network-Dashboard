-- 002: cancellation fields for change approvals (additive; existing rows untouched).
-- status is a plain varchar, so 'cancelled' needs no type change. Approval rows are never
-- deleted; cancellation is a terminal status recorded with who/when/why.
--
-- Apply BEFORE deploying the network-api image that reads these columns.
-- Rollback: 002_approval_cancellation.down.sql
-- Safe to re-run.

BEGIN;

ALTER TABLE change_approvals
    ADD COLUMN IF NOT EXISTS cancelled_by        varchar(64),
    ADD COLUMN IF NOT EXISTS cancelled_at        timestamptz,
    ADD COLUMN IF NOT EXISTS cancellation_reason text;

DO $$
BEGIN
    -- A cancelled row must always record who cancelled it and when.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'change_approvals_cancelled_fields_chk'
          AND conrelid = 'change_approvals'::regclass
    ) THEN
        ALTER TABLE change_approvals
            ADD CONSTRAINT change_approvals_cancelled_fields_chk
            CHECK (status <> 'cancelled' OR (cancelled_by IS NOT NULL AND cancelled_at IS NOT NULL));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'change_approvals_cancellation_reason_len_chk'
          AND conrelid = 'change_approvals'::regclass
    ) THEN
        ALTER TABLE change_approvals
            ADD CONSTRAINT change_approvals_cancellation_reason_len_chk
            CHECK (cancellation_reason IS NULL OR length(cancellation_reason) <= 500);
    END IF;
END $$;

COMMIT;

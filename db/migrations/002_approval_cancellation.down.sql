-- Rollback for 002_approval_cancellation.sql.
-- Refuses to run while cancelled approvals exist: dropping the columns would erase who
-- cancelled them and why. Roll network-api back to an image without cancellation first.
BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM change_approvals WHERE status = 'cancelled') THEN
        RAISE EXCEPTION 'cancelled approvals exist; refusing to drop cancellation history';
    END IF;
END $$;

ALTER TABLE change_approvals
    DROP CONSTRAINT IF EXISTS change_approvals_cancellation_reason_len_chk,
    DROP CONSTRAINT IF EXISTS change_approvals_cancelled_fields_chk,
    DROP COLUMN IF EXISTS cancellation_reason,
    DROP COLUMN IF EXISTS cancelled_at,
    DROP COLUMN IF EXISTS cancelled_by;

COMMIT;

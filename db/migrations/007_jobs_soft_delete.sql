-- 007: manual job deletion = soft delete (additive; no existing column or FK is touched).
--
-- Job rows are referenced by backups.job_id, audit_events.job_id and
-- change_approvals.backup_job_id (the approval's pre-change backup, verified at apply
-- time). Those base tables pre-date these migrations, so their ON DELETE behaviour is not
-- guaranteed here: a hard DELETE could fail, or (with CASCADE) remove backups, approvals
-- and audit history. "Delete" therefore only hides a finished job from the Jobs list;
-- every related row and file stays exactly as it was.
--
-- Apply:    psql -v ON_ERROR_STOP=1 -U networkapp -d networkdb -f 007_jobs_soft_delete.sql
-- Rollback: 007_jobs_soft_delete.down.sql (deleted jobs reappear in the list)
-- Safe to re-run.

BEGIN;

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS deleted_by varchar(64);

CREATE INDEX IF NOT EXISTS jobs_visible_started_idx ON jobs (started_at DESC) WHERE deleted_at IS NULL;

COMMIT;

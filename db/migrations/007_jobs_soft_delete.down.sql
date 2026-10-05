-- Rollback for 007_jobs_soft_delete.sql. Deploy an API without job deletion first
-- (the job list query reads deleted_at). Previously deleted jobs become visible again;
-- the job_deleted audit events remain.
BEGIN;
DROP INDEX IF EXISTS jobs_visible_started_idx;
ALTER TABLE jobs DROP COLUMN IF EXISTS deleted_by;
ALTER TABLE jobs DROP COLUMN IF EXISTS deleted_at;
COMMIT;

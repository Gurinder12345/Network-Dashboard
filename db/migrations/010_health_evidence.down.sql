-- Rollback for 010_health_evidence.sql. Drops only the added evidence columns (rewritten
-- by the next health sweep if re-applied). Deploy a worker/API without them first, or the
-- new worker simply falls back to the original columns (it re-checks every 5 minutes).
BEGIN;
ALTER TABLE device_health
    DROP COLUMN IF EXISTS icmp_reachable,
    DROP COLUMN IF EXISTS health_reason,
    DROP COLUMN IF EXISTS unreachable_count;
COMMIT;

-- 010: health evidence + reason codes (additive; existing columns and rows unchanged).
--
--   icmp_reachable     ICMP echo reply on the last check (NULL = ICMP could not be attempted)
--   health_reason      stable machine-readable reason for the status (ok,
--                      ssh_management_unavailable, ssh_authentication_failed, ssh_timeout,
--                      ssh_session_failed, cli_verification_failed, network_unreachable,
--                      tcp22_unreachable, slow_response, unsupported_platform)
--   unreachable_count  consecutive checks with neither TCP/22 nor an ICMP reply; only this
--                      streak can make a device "down" (consecutive_failures keeps counting
--                      every failed check, as before)
--
-- tcp_reachable keeps its name and means TCP port 22 only.
-- After applying, restart network-worker so every worker process starts writing the new
-- columns immediately (a running process re-checks for them only every 5 minutes).
-- A worker without this migration keeps writing the original columns (it checks first).
--
-- Apply:    psql -v ON_ERROR_STOP=1 -U networkapp -d networkdb -f 010_health_evidence.sql
-- Rollback: 010_health_evidence.down.sql
-- Safe to re-run.

BEGIN;

ALTER TABLE device_health
    ADD COLUMN IF NOT EXISTS icmp_reachable    boolean,
    ADD COLUMN IF NOT EXISTS health_reason     varchar(48),
    ADD COLUMN IF NOT EXISTS unreachable_count integer NOT NULL DEFAULT 0;

-- Continuity for rows last written before this migration (health_reason still NULL):
-- seed the streak exactly as the worker's pre-010 fallback does (consecutive_failures while
-- TCP/22 is failing). Without it, a device that is already DOWN would read
-- unreachable_count = 0 and drop to "degraded (1 of N)" for one sweep. Rows written by the
-- new worker (health_reason set) are never touched, so re-running is safe.
UPDATE device_health
SET unreachable_count = consecutive_failures
WHERE health_reason IS NULL
  AND tcp_reachable = false
  AND unreachable_count = 0
  AND consecutive_failures > 0;

COMMIT;

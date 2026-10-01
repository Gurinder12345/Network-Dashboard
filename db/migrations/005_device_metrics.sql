-- 005: CPU / memory telemetry history (additive; devices and device_health are untouched).
--
-- One row per collection attempt per device, written by the worker's telemetry task.
-- Failed attempts are stored with NULL metrics (never 0) so history shows a gap and the
-- last error stays visible. Telemetry never changes device health.
--
-- Growth: 10 devices x 1 sample/min = 14,400 rows/day (~5.3 M/year). Fine for the lab;
-- a retention job can be added later (DELETE ... WHERE collected_at < now() - interval).
--
-- Apply:    psql -v ON_ERROR_STOP=1 -U networkapp -d networkdb -f 005_device_metrics.sql
-- Rollback: 005_device_metrics.down.sql
-- Safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS device_metrics (
    id                bigserial    PRIMARY KEY,
    device_id         bigint       NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    collected_at      timestamptz  NOT NULL,
    cpu_percent       numeric(5,2) CHECK (cpu_percent IS NULL OR cpu_percent BETWEEN 0 AND 100),
    memory_percent    numeric(5,2) CHECK (memory_percent IS NULL OR memory_percent BETWEEN 0 AND 100),
    memory_used_mb    numeric(12,1) CHECK (memory_used_mb IS NULL OR memory_used_mb >= 0),
    memory_total_mb   numeric(12,1) CHECK (memory_total_mb IS NULL OR memory_total_mb > 0),
    uptime_seconds    bigint       CHECK (uptime_seconds IS NULL OR uptime_seconds >= 0),
    source            varchar(255) NOT NULL,
    collection_status varchar(16)  NOT NULL CHECK (collection_status IN ('success', 'partial', 'failed')),
    error             text         CHECK (error IS NULL OR length(error) <= 1024),
    created_at        timestamptz  NOT NULL DEFAULT now(),
    -- success = CPU and memory; partial = exactly one of them; failed = neither.
    CONSTRAINT device_metrics_status_matches_values CHECK (
        (collection_status = 'success' AND cpu_percent IS NOT NULL AND memory_percent IS NOT NULL)
        OR (collection_status = 'partial' AND (cpu_percent IS NULL) <> (memory_percent IS NULL))
        OR (collection_status = 'failed' AND cpu_percent IS NULL AND memory_percent IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS device_metrics_device_time_idx
    ON device_metrics (device_id, collected_at DESC);

COMMIT;

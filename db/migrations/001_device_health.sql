-- 001: current health state per device (additive; devices is not modified).
-- One row per device, written by network-worker health checks. PostgreSQL is the
-- source of truth; Redis only caches these rows.
--
-- Apply:    psql -v ON_ERROR_STOP=1 -U networkapp -d networkdb -f 001_device_health.sql
-- Rollback: 001_device_health.down.sql
-- Safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS device_health (
    device_id             bigint       PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
    status                varchar(16)  NOT NULL DEFAULT 'unknown'
                          CHECK (status IN ('healthy', 'degraded', 'down', 'unknown')),
    last_check_at         timestamptz,
    last_success_at       timestamptz,
    response_time_ms      integer,
    tcp_reachable         boolean      NOT NULL DEFAULT false,
    ssh_reachable         boolean      NOT NULL DEFAULT false,
    cli_reachable         boolean      NOT NULL DEFAULT false,
    last_error            text         CHECK (last_error IS NULL OR length(last_error) <= 2048),
    consecutive_failures  integer      NOT NULL DEFAULT 0,
    last_status_change_at timestamptz,
    updated_at            timestamptz  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS device_health_status_idx ON device_health (status);

COMMIT;

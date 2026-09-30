-- 003: LLDP topology (additive; devices and device_health are untouched).
--
-- topology_links stores DIRECTIONAL observations: "local device/interface saw this
-- neighbor". The API folds the two directions of one physical link into a single edge.
--
-- Uniqueness: one row per (local device, local interface, protocol, remote chassis,
-- remote port). A single access port can legitimately report several LLDP neighbors
-- (e.g. an IP phone and the PC behind it, or an unmanaged switch), so the local port
-- alone is not unique. NULLS NOT DISTINCT (PostgreSQL 15+) makes a missing chassis/port
-- value compare equal, so re-observations update instead of duplicating.
--
-- Apply:    psql -v ON_ERROR_STOP=1 -U networkapp -d networkdb -f 003_topology.sql
-- Rollback: 003_topology.down.sql
-- Safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS topology_links (
    id                   uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    local_device_id      bigint       NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    local_interface      varchar(128) NOT NULL,
    remote_device_id     bigint       REFERENCES devices(id) ON DELETE SET NULL,
    remote_system_name   varchar(255),
    remote_interface     varchar(128),
    remote_management_ip inet,
    remote_chassis_id    varchar(255),
    remote_port_id       varchar(255),
    remote_capabilities  text,
    protocol             varchar(32)  NOT NULL DEFAULT 'lldp',
    discovery_source     varchar(64),
    first_seen_at        timestamptz  NOT NULL,
    last_seen_at         timestamptz  NOT NULL,
    active               boolean      NOT NULL DEFAULT true,
    CONSTRAINT topology_links_observation_uq UNIQUE NULLS NOT DISTINCT
        (local_device_id, local_interface, protocol, remote_chassis_id, remote_port_id)
);

CREATE INDEX IF NOT EXISTS topology_links_local_device_idx  ON topology_links (local_device_id);
CREATE INDEX IF NOT EXISTS topology_links_remote_device_idx ON topology_links (remote_device_id);
CREATE INDEX IF NOT EXISTS topology_links_active_idx        ON topology_links (active);
CREATE INDEX IF NOT EXISTS topology_links_last_seen_idx     ON topology_links (last_seen_at);

-- Discovery status per device, deliberately separate from device_health: a switch can
-- be healthy while its LLDP collection fails.
CREATE TABLE IF NOT EXISTS topology_discovery_state (
    device_id        bigint      PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
    last_attempt_at  timestamptz,
    last_success_at  timestamptz,
    last_error       text        CHECK (last_error IS NULL OR length(last_error) <= 2048),
    neighbors_seen   integer,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

COMMIT;

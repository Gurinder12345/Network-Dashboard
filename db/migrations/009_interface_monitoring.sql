-- 009: Interface Monitoring V1 (additive; no existing table is altered).
--
-- interface_inventory   one row per (device, canonical interface name): the latest
--                       normalized state from read-only `show` commands. Interfaces that
--                       disappear are kept (last_seen stops advancing), never deleted.
-- interface_metrics     one row per interface per successful collection: raw counters as
--                       reported by the switch plus utilization derived from the counter
--                       delta to the previous sample (NULL when no valid delta exists:
--                       first sample, counter reset/wrap, unknown speed, long gap).
--
-- PostgreSQL is the source of truth; Redis only holds the latest per-device snapshot.
-- Interface collection never changes device health.
--
-- Growth (5-minute polling, ~50 interfaces/device): 10 devices -> 144,000 rows/day.
-- No retention job is installed by this migration (see the release notes for the plan).
--
-- Apply:    psql -v ON_ERROR_STOP=1 -U networkapp -d networkdb -f 009_interface_monitoring.sql
-- Rollback: 009_interface_monitoring.down.sql (drops collected interface history)
-- Safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS interface_inventory (
    id                bigserial    PRIMARY KEY,
    device_id         bigint       NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    interface_name    varchar(64)  NOT NULL,   -- as displayed by the switch (e.g. Te1/0/1, Eth 1/1/1)
    canonical_name    varchar(64)  NOT NULL,   -- normalized identity (e.g. te1/0/1, ethernet1/1/1)
    description       varchar(255),
    interface_type    varchar(16)  NOT NULL CHECK (interface_type IN ('ethernet', 'port_channel', 'management', 'other')),
    admin_status      varchar(8)   CHECK (admin_status IS NULL OR admin_status IN ('up', 'down')),
    oper_status       varchar(8)   CHECK (oper_status IS NULL OR oper_status IN ('up', 'down')),
    speed_bps         bigint       CHECK (speed_bps IS NULL OR speed_bps > 0),
    duplex            varchar(8)   CHECK (duplex IS NULL OR duplex IN ('full', 'half')),
    mode              varchar(16)  CHECK (mode IS NULL OR mode IN ('access', 'trunk', 'general', 'routed')),
    access_vlan       integer      CHECK (access_vlan IS NULL OR access_vlan BETWEEN 1 AND 4094),
    native_vlan       integer      CHECK (native_vlan IS NULL OR native_vlan BETWEEN 1 AND 4094),
    allowed_vlans     varchar(1024),
    port_channel      varchar(64),
    mtu               integer      CHECK (mtu IS NULL OR mtu > 0),
    role              varchar(16)  CHECK (role IS NULL OR role IN ('inter_switch', 'uplink', 'lag')),
    last_state_change timestamptz,
    first_seen        timestamptz  NOT NULL,
    last_seen         timestamptz  NOT NULL,
    created_at        timestamptz  NOT NULL DEFAULT now(),
    updated_at        timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT interface_inventory_device_name_uq UNIQUE (device_id, canonical_name)
);

CREATE TABLE IF NOT EXISTS interface_metrics (
    id                 bigserial    PRIMARY KEY,
    device_id          bigint       NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    interface_id       bigint       NOT NULL REFERENCES interface_inventory(id) ON DELETE CASCADE,
    collected_at       timestamptz  NOT NULL,
    rx_bytes           bigint       CHECK (rx_bytes IS NULL OR rx_bytes >= 0),
    tx_bytes           bigint       CHECK (tx_bytes IS NULL OR tx_bytes >= 0),
    rx_packets         bigint       CHECK (rx_packets IS NULL OR rx_packets >= 0),
    tx_packets         bigint       CHECK (tx_packets IS NULL OR tx_packets >= 0),
    rx_errors          bigint       CHECK (rx_errors IS NULL OR rx_errors >= 0),
    tx_errors          bigint       CHECK (tx_errors IS NULL OR tx_errors >= 0),
    crc_errors         bigint       CHECK (crc_errors IS NULL OR crc_errors >= 0),
    input_discards     bigint       CHECK (input_discards IS NULL OR input_discards >= 0),
    output_discards    bigint       CHECK (output_discards IS NULL OR output_discards >= 0),
    rx_utilization_pct numeric(5,2) CHECK (rx_utilization_pct IS NULL OR rx_utilization_pct BETWEEN 0 AND 100),
    tx_utilization_pct numeric(5,2) CHECK (tx_utilization_pct IS NULL OR tx_utilization_pct BETWEEN 0 AND 100)
);

-- Interface history and "latest sample per interface" (fallback when Redis has no snapshot).
CREATE INDEX IF NOT EXISTS interface_metrics_interface_time_idx
    ON interface_metrics (interface_id, collected_at DESC);

-- Per-device reads: previous counters for the utilization delta, device time ranges.
CREATE INDEX IF NOT EXISTS interface_metrics_device_time_idx
    ON interface_metrics (device_id, collected_at DESC);

COMMIT;

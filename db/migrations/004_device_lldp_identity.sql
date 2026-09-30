-- 004: operator-maintained LLDP identities for managed devices (additive).
--
-- Maps an advertised LLDP system name and/or chassis ID to an existing device, so
-- correlation is explicit and deterministic (no fuzzy matching). Rows are written only
-- by an operator after confirmation; discovery never creates them.
--
-- Normalization contract (enforced here, applied by the worker before comparing):
--   lldp_system_name  stored as advertised but trimmed; compared case-insensitively.
--   chassis_id        stored lowercase and trimmed; a MAC address must be in canonical
--                     colon form (aa:bb:cc:dd:ee:ff) -- dashed, dotted or bare-hex MAC
--                     forms are rejected so one MAC can only be written one way.
--
-- Apply:    psql -v ON_ERROR_STOP=1 -U networkapp -d networkdb -f 004_device_lldp_identity.sql
-- Rollback: 004_device_lldp_identity.down.sql
-- Safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS device_lldp_identity (
    id               uuid         PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id        bigint       NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    lldp_system_name varchar(255),
    chassis_id       varchar(255),
    note             text,
    created_at       timestamptz  NOT NULL DEFAULT now(),
    updated_at       timestamptz  NOT NULL DEFAULT now(),

    CONSTRAINT device_lldp_identity_has_value_chk
        CHECK (lldp_system_name IS NOT NULL OR chassis_id IS NOT NULL),

    CONSTRAINT device_lldp_identity_name_trimmed_chk
        CHECK (lldp_system_name IS NULL OR (lldp_system_name = btrim(lldp_system_name) AND lldp_system_name <> '')),

    CONSTRAINT device_lldp_identity_chassis_normalized_chk
        CHECK (
            chassis_id IS NULL OR (
                chassis_id = lower(btrim(chassis_id))
                AND chassis_id <> ''
                AND chassis_id !~ '^[0-9a-f]{12}$'
                AND chassis_id !~ '^([0-9a-f]{2}-){5}[0-9a-f]{2}$'
                AND chassis_id !~ '^([0-9a-f]{4}\.){2}[0-9a-f]{4}$'
            )
        )
);

-- One advertised name / one chassis can identify only one device.
-- A device may have several identity rows (e.g. two chassis IDs, or a name row).
CREATE UNIQUE INDEX IF NOT EXISTS device_lldp_identity_name_uq
    ON device_lldp_identity (lower(lldp_system_name)) WHERE lldp_system_name IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS device_lldp_identity_chassis_uq
    ON device_lldp_identity (chassis_id) WHERE chassis_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS device_lldp_identity_device_idx
    ON device_lldp_identity (device_id);

COMMIT;

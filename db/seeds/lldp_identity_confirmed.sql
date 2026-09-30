-- CONFIRMED LLDP identities. Each row is backed by a reciprocal LLDP port pair in the
-- saved real captures (apps/network-worker/tests/fixtures/*real*):
--
--   kenda-core-01 / e8:b5:d0:7a:61:a3 -> Kenda-Core-1
--       Core-2 ethernet1/1/25 sees kenda-core-01 port ethernet1/1/25
--       Core-1 ethernet1/1/25 sees kenda-core-02 port ethernet1/1/25   (reciprocal)
--   kenda-core-02 / e8:b5:d0:7a:5c:a3 -> Kenda-Core-2                  (same link, other side)
--   HARO_SW_01    / f0:d4:e2:95:6b:1d -> Kenda-HARO-SW-01
--       HARO-IDF-A Tw1/0/4 sees HARO_SW_01 port Te1/0/3
--       HARO-SW-01 Te1/0/3 sees HARO-IDF-A port Tw1/0/4                 (reciprocal)
--   HARO-IDF-A    / e8:b2:65:4c:c2:68 -> Kenda-HARO-IDF-A                (same link, other side)
--
-- NOT included (no saved reciprocal evidence / no inventory device):
--   HARO_SW_02 (seen from HARO-SW-01 Gi1/0/1; no saved SW-02 capture), Haro-IDF-B (no
--   inventory device), HQ-KENDA-2, KENDA-HQ-1, Kenda-HQ (HQ switches down).
--
-- Idempotent: an identical existing row is skipped. A CONFLICTING row (same name or
-- chassis already mapped elsewhere) violates a unique index and aborts the transaction.
-- Run with psql -v ON_ERROR_STOP=1.

BEGIN;

DO $$
DECLARE
    missing text;
BEGIN
    SELECT string_agg(h, ', ') INTO missing
    FROM unnest(ARRAY['Kenda-Core-1', 'Kenda-Core-2', 'Kenda-HARO-SW-01', 'Kenda-HARO-IDF-A']) AS h
    WHERE NOT EXISTS (SELECT 1 FROM devices d WHERE d.hostname = h);

    IF missing IS NOT NULL THEN
        RAISE EXCEPTION 'inventory hostnames not found: %', missing;
    END IF;
END $$;

INSERT INTO device_lldp_identity (device_id, lldp_system_name, chassis_id, note)
SELECT d.id, v.name, v.chassis, v.note
FROM (VALUES
    ('Kenda-Core-1',     'kenda-core-01', 'e8:b5:d0:7a:61:a3', 'Confirmed by reciprocal LLDP: Core-2 e1/1/25 <-> Core-1 e1/1/25'),
    ('Kenda-Core-2',     'kenda-core-02', 'e8:b5:d0:7a:5c:a3', 'Confirmed by reciprocal LLDP: Core-1 e1/1/25 <-> Core-2 e1/1/25'),
    ('Kenda-HARO-SW-01', 'HARO_SW_01',    'f0:d4:e2:95:6b:1d', 'Confirmed by reciprocal LLDP: HARO-IDF-A Tw1/0/4 <-> HARO-SW-01 Te1/0/3'),
    ('Kenda-HARO-IDF-A', 'HARO-IDF-A',    'e8:b2:65:4c:c2:68', 'Confirmed by reciprocal LLDP: HARO-SW-01 Te1/0/3 <-> HARO-IDF-A Tw1/0/4')
) AS v(hostname, name, chassis, note)
JOIN devices d ON d.hostname = v.hostname
WHERE NOT EXISTS (
    SELECT 1 FROM device_lldp_identity i
    WHERE i.device_id = d.id
      AND lower(i.lldp_system_name) = lower(v.name)
      AND i.chassis_id = v.chassis
);

COMMIT;

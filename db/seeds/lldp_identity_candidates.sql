-- LLDP identity CANDIDATES. Every statement is commented out on purpose.
-- Uncomment a block ONLY after deterministic confirmation (reciprocal LLDP port pair in a
-- capture from the managed device, or direct CLI verification on that device), e.g.:
--   python3 -m topology.identity_report --capture <A>=... --capture <B>=... --inventory-url ...
-- Chassis IDs must be canonical lowercase colon form (the migration rejects other forms).

-- kenda-core-02 (seen on Kenda-Core-1 ethernet1/1/25 -> remote ethernet1/1/25)
-- Confirm with Kenda-Core-2 `show lldp neighbors`: ethernet1/1/25 must report remote port ethernet1/1/25.
-- INSERT INTO device_lldp_identity (device_id, lldp_system_name, chassis_id, note)
-- SELECT id, 'kenda-core-02', 'e8:b5:d0:7a:5c:a3', 'confirmed by reciprocal LLDP <date>'
-- FROM devices WHERE hostname = 'Kenda-Core-2';

-- HQ-KENDA-2 (Core-1 ethernet1/1/26:1 -> Te1/0/3, 26:4 -> Te1/0/4), chassis e8:b5:d0:cd:0c:cb
-- Confirm: the HQ switch whose Te1/0/3 reports remote port ethernet1/1/26:1. Device NOT yet known.
-- INSERT INTO device_lldp_identity (device_id, lldp_system_name, chassis_id, note)
-- SELECT id, 'HQ-KENDA-2', 'e8:b5:d0:cd:0c:cb', 'confirmed by reciprocal LLDP <date>'
-- FROM devices WHERE hostname = '<CONFIRMED HQ HOSTNAME>';

-- KENDA-HQ-1 (Core-1 ethernet1/1/26:2 -> Te1/0/2, 26:3 -> Te1/0/1), chassis e8:b5:d0:cd:04:cb
-- Confirm: the HQ switch whose Te1/0/2 reports remote port ethernet1/1/26:2. Device NOT yet known.
-- INSERT INTO device_lldp_identity (device_id, lldp_system_name, chassis_id, note)
-- SELECT id, 'KENDA-HQ-1', 'e8:b5:d0:cd:04:cb', 'confirmed by reciprocal LLDP <date>'
-- FROM devices WHERE hostname = '<CONFIRMED HQ HOSTNAME>';

-- HARO_SW_01 (Kenda-HARO-IDF-A Tw1/0/4 -> Te1/0/3), chassis f0:d4:e2:95:6b:1d
-- Confirm with Kenda-HARO-SW-01 `show lldp remote-device all`: Te1/0/3 must report remote port Tw1/0/4.
-- INSERT INTO device_lldp_identity (device_id, lldp_system_name, chassis_id, note)
-- SELECT id, 'HARO_SW_01', 'f0:d4:e2:95:6b:1d', 'confirmed by reciprocal LLDP <date>'
-- FROM devices WHERE hostname = 'Kenda-HARO-SW-01';

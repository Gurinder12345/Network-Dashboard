-- Rollback for 004_device_lldp_identity.sql.
-- Removes operator-entered LLDP identity mappings. Correlation falls back to
-- management IP / exact hostname only. Export the rows first if they should be kept:
--   \copy (SELECT * FROM device_lldp_identity) TO 'device_lldp_identity_backup.csv' CSV HEADER
BEGIN;
DROP TABLE IF EXISTS device_lldp_identity;
COMMIT;

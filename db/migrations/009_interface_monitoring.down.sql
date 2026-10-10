-- Rollback for 009_interface_monitoring.sql.
--
-- DESTRUCTIVE: removes all collected interface inventory and history (it cannot be
-- re-created for the past). Prefer leaving the tables in place when rolling back the
-- application: older worker/API images simply do not read or write them.
-- If they must go: disable interface polling (INTERFACE_BEAT_ENABLED=false) and deploy
-- images without interface monitoring first, take a pg_dump of both tables, then run this.
BEGIN;
DROP TABLE IF EXISTS interface_metrics;
DROP TABLE IF EXISTS interface_inventory;
COMMIT;

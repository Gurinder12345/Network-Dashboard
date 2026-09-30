-- Rollback for 001_device_health.sql. Removes only health state; devices and all other
-- tables are untouched. Stop the health Beat schedule first or the worker will log
-- write failures until the table exists again.
BEGIN;
DROP TABLE IF EXISTS device_health;
COMMIT;

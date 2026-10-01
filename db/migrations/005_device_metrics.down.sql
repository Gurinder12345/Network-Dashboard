-- Rollback for 005_device_metrics.sql. Removes only telemetry history (re-collected by the worker).
-- Remove the telemetry Beat entry / deploy a worker without telemetry tasks first.
BEGIN;
DROP TABLE IF EXISTS device_metrics;
COMMIT;

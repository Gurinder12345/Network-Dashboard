-- Rollback for 003_topology.sql. Removes only topology data (rebuildable by rediscovery).
-- Remove the topology Beat entry / deploy a worker without topology tasks first.
BEGIN;
DROP TABLE IF EXISTS topology_discovery_state;
DROP TABLE IF EXISTS topology_links;
COMMIT;

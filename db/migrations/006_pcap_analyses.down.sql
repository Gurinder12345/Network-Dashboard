-- Rollback for 006_pcap_analyses.sql. Removes stored analysis results only (uploads are
-- temporary files under /pcap-analysis and are not affected by this script).
BEGIN;
DROP TABLE IF EXISTS pcap_analyses;
COMMIT;

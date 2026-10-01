-- 006: PCAP analyzer results (additive; no existing table is touched).
--
-- One row per uploaded analysis. Raw packets are NEVER stored here: the uploaded files
-- live under /pcap-analysis/<id>/ only until the analysis ends (and at most until
-- expires_at, enforced by the hourly cleanup task). result_json holds the structured,
-- payload-free analysis (flows, findings, assessment) and is kept until deleted.
--
-- Apply:    psql -v ON_ERROR_STOP=1 -U networkapp -d networkdb -f 006_pcap_analyses.sql
-- Rollback: 006_pcap_analyses.down.sql
-- Safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS pcap_analyses (
    id                        uuid          PRIMARY KEY,
    status                    varchar(16)   NOT NULL DEFAULT 'queued'
                              CHECK (status IN ('queued', 'validating', 'extracting', 'analyzing', 'correlating', 'completed', 'failed')),
    mode                      varchar(8)    NOT NULL CHECK (mode IN ('single', 'dual')),
    created_at                timestamptz   NOT NULL DEFAULT now(),
    started_at                timestamptz,
    completed_at              timestamptz,
    updated_at                timestamptz   NOT NULL DEFAULT now(),
    -- Display names only (sanitized). Files on disk use fixed internal names.
    client_filename           varchar(255)  NOT NULL,
    server_filename           varchar(255),
    client_file               varchar(16)   NOT NULL CHECK (client_file IN ('client.pcap', 'client.pcapng')),
    server_file               varchar(16)   CHECK (server_file IS NULL OR server_file IN ('server.pcap', 'server.pcapng')),
    client_size_bytes         bigint        NOT NULL CHECK (client_size_bytes > 0),
    server_size_bytes         bigint        CHECK (server_size_bytes IS NULL OR server_size_bytes > 0),
    capture_duration_seconds  double precision,
    packet_count              bigint,
    flow_count                integer,
    likely_issue              varchar(255),
    finding_counts            jsonb,
    result_json               jsonb,
    error                     text          CHECK (error IS NULL OR length(error) <= 1024),
    expires_at                timestamptz   NOT NULL,
    files_deleted_at          timestamptz,
    created_by                varchar(64)   NOT NULL,
    CONSTRAINT pcap_analyses_mode_files CHECK (
        (mode = 'single' AND server_file IS NULL AND server_filename IS NULL AND server_size_bytes IS NULL)
        OR (mode = 'dual' AND server_file IS NOT NULL AND server_filename IS NOT NULL AND server_size_bytes IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS pcap_analyses_created_idx ON pcap_analyses (created_at DESC);
CREATE INDEX IF NOT EXISTS pcap_analyses_active_idx ON pcap_analyses (status)
    WHERE status NOT IN ('completed', 'failed');

COMMIT;

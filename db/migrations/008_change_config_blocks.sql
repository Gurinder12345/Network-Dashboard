-- Ordered multi-block configuration changes (Dell OS6 + Dell OS10).
--
-- Additive and nullable: existing rows keep config_lines / config_parents and are shown
-- through the API's compatibility adapter (one block). New rows store:
--   config_blocks          ordered [{order, parent|null, commands[]}] (source of truth)
--   verification_commands  optional read-only `show ...` commands run after apply
--   precheck_summary       per-block precheck result shown to the approver
--   execution_result       per-block execution progress, outcome and post-check
-- config_lines stays NOT NULL and holds a flat CLI rendering for older readers.
BEGIN;

ALTER TABLE change_approvals
    ADD COLUMN IF NOT EXISTS config_blocks         jsonb,
    ADD COLUMN IF NOT EXISTS verification_commands jsonb,
    ADD COLUMN IF NOT EXISTS precheck_summary      jsonb,
    ADD COLUMN IF NOT EXISTS execution_result      jsonb;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'change_approvals_config_blocks_chk'
                   AND conrelid = 'change_approvals'::regclass) THEN
        ALTER TABLE change_approvals ADD CONSTRAINT change_approvals_config_blocks_chk
            CHECK (config_blocks IS NULL OR (jsonb_typeof(config_blocks) = 'array' AND jsonb_array_length(config_blocks) > 0));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'change_approvals_verification_commands_chk'
                   AND conrelid = 'change_approvals'::regclass) THEN
        ALTER TABLE change_approvals ADD CONSTRAINT change_approvals_verification_commands_chk
            CHECK (verification_commands IS NULL OR jsonb_typeof(verification_commands) = 'array');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'change_approvals_execution_result_chk'
                   AND conrelid = 'change_approvals'::regclass) THEN
        ALTER TABLE change_approvals ADD CONSTRAINT change_approvals_execution_result_chk
            CHECK (execution_result IS NULL OR jsonb_typeof(execution_result) = 'object');
    END IF;
END $$;

COMMIT;

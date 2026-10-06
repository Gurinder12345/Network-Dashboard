-- Rollback of 008. Drops the multi-block data of changes created after 008 (their flat
-- config_lines rendering is kept, so they still display as one block).
BEGIN;

ALTER TABLE change_approvals
    DROP CONSTRAINT IF EXISTS change_approvals_config_blocks_chk,
    DROP CONSTRAINT IF EXISTS change_approvals_verification_commands_chk,
    DROP CONSTRAINT IF EXISTS change_approvals_execution_result_chk;

ALTER TABLE change_approvals
    DROP COLUMN IF EXISTS execution_result,
    DROP COLUMN IF EXISTS precheck_summary,
    DROP COLUMN IF EXISTS verification_commands,
    DROP COLUMN IF EXISTS config_blocks;

COMMIT;

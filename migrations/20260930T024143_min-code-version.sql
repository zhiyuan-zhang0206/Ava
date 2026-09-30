-- Client-side code-version gate (decisions/2026-09-30-client-side-code-version-gate.md):
-- the lowest code version allowed to write. Every gateway start raises it to that
-- gateway's own version (GREATEST, so a rollback never lowers it); every pooled
-- session reads it and a process running older code exits. The version is the
-- first-parent commit count of the code the process loaded. 0 = nothing
-- recorded yet, which no process is below.
ALTER TABLE deployment_state ADD COLUMN IF NOT EXISTS min_code_version BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN deployment_state.min_code_version IS
    'Lowest code version (first-parent commit count of the process''s loaded commit) allowed to write; every gateway start raises it with GREATEST, every pooled session reads it and a lower process exits (decisions/2026-09-30-client-side-code-version-gate.md). Lowered only by hand after a rollback.';

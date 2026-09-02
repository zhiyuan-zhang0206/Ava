-- Nullable evidence belongs to the existing rollout, not another registry.
ALTER TABLE deployment_state ADD COLUMN IF NOT EXISTS managed_writer_evidence JSONB;

COMMENT ON COLUMN deployment_state.managed_writer_evidence IS
    'Versioned operation-bound managed-writer closure evidence; NULL is unknown, never permission.';

-- Return the cluster default to the flash tier (user ruling 2026-09-10: all
-- Ava-line agents run deepseek-v4-flash; the vision experiment is withdrawn).
-- Advance only the value installed by the prior migration or the baseline; an
-- API-owned choice is an explicit operator selection and must survive.
UPDATE cluster_defaults
SET llm_model = 'deepseek-v4-flash', updated_at = now(), updated_by = 'migration'
WHERE id = 1
  AND llm_model = 'deepseek-v4-flash-vision-exp'
  AND (updated_by IS NULL OR updated_by = 'migration');

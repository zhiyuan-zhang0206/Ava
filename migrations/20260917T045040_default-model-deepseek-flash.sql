-- Advance the cluster default to the flash tier's canonical id (user report
-- 2026-09-17, task #3750: the provider renamed the tier to `deepseek-flash`;
-- the retired `deepseek-v4-flash` id resolves to it before provider
-- construction). Advance only the value installed by the prior migration or
-- the baseline; an API-owned choice is an explicit operator selection and must
-- survive.
UPDATE cluster_defaults
SET llm_model = 'deepseek-flash', updated_at = now(), updated_by = 'migration'
WHERE id = 1
  AND llm_model = 'deepseek-v4-flash'
  AND (updated_by IS NULL OR updated_by = 'migration');

-- Advance only the cluster default installed by the prior migration or baseline.
-- An API-owned flash selection is an explicit operator choice and must survive.
UPDATE cluster_defaults
SET llm_model = 'deepseek-v4-flash-vision-exp', updated_at = now(), updated_by = 'migration'
WHERE id = 1
  AND llm_model = 'deepseek-v4-flash'
  AND (updated_by IS NULL OR updated_by = 'migration');

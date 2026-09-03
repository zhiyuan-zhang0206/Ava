-- Reverse only the value this migration installed; preserve later operator choices.
UPDATE cluster_defaults
SET llm_model = 'deepseek-v4-flash', updated_at = now(), updated_by = 'migration'
WHERE id = 1
  AND llm_model = 'deepseek-v4-flash-vision-exp'
  AND updated_by = 'migration';

-- The gate reads this column, so code that carries it fails on a schema without
-- it: roll the code back first (or together with this down migration).
ALTER TABLE deployment_state DROP COLUMN IF EXISTS min_code_version;

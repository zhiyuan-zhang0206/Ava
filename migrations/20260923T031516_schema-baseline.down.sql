-- A reset is the rollback floor: the folded strict history cannot be replayed.
DO $$ BEGIN
    RAISE EXCEPTION 'cannot roll back across the 2026-09-23 schema baseline; fix forward';
END $$;

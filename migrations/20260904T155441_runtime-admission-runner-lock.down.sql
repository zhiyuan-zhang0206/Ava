DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        REVOKE EXECUTE ON FUNCTION public.lock_runtime_publication_admission() FROM ava_runner;
    END IF;
END
$$;

DROP FUNCTION IF EXISTS public.lock_runtime_publication_admission();

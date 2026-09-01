-- Retire the one-time correction snapshots only after their contents have
-- been archived. These rows are the recovery source for the corrections that
-- created them, so a populated snapshot must fail loudly rather than vanish.
--
-- Check every snapshot before any DROP. The current baseline already omits
-- these tables, so an absent table is equivalent to an empty retired snapshot
-- and keeps fresh-bootstrap replayable.

DO $$
DECLARE
    snapshot_has_rows BOOLEAN;
BEGIN
    IF to_regclass('public.fork_lineage_fix_backfill_agents_meta') IS NOT NULL THEN
        EXECUTE 'LOCK TABLE public.fork_lineage_fix_backfill_agents_meta IN ACCESS EXCLUSIVE MODE';
        EXECUTE 'SELECT EXISTS (SELECT 1 FROM public.fork_lineage_fix_backfill_agents_meta)'
            INTO snapshot_has_rows;
        IF snapshot_has_rows THEN
            RAISE EXCEPTION
                'fork_lineage_fix_backfill_agents_meta must be archived before retirement';
        END IF;
    END IF;

    IF to_regclass('public.fork_lineage_fix_backfill_events') IS NOT NULL THEN
        EXECUTE 'LOCK TABLE public.fork_lineage_fix_backfill_events IN ACCESS EXCLUSIVE MODE';
        EXECUTE 'SELECT EXISTS (SELECT 1 FROM public.fork_lineage_fix_backfill_events)'
            INTO snapshot_has_rows;
        IF snapshot_has_rows THEN
            RAISE EXCEPTION
                'fork_lineage_fix_backfill_events must be archived before retirement';
        END IF;
    END IF;

    IF to_regclass('public.ledger_unpriced_backfill_20260824') IS NOT NULL THEN
        EXECUTE 'LOCK TABLE public.ledger_unpriced_backfill_20260824 IN ACCESS EXCLUSIVE MODE';
        EXECUTE 'SELECT EXISTS (SELECT 1 FROM public.ledger_unpriced_backfill_20260824)'
            INTO snapshot_has_rows;
        IF snapshot_has_rows THEN
            RAISE EXCEPTION
                'ledger_unpriced_backfill_20260824 must be archived before retirement';
        END IF;
    END IF;
END $$;

DROP TABLE IF EXISTS public.fork_lineage_fix_backfill_agents_meta;
DROP TABLE IF EXISTS public.fork_lineage_fix_backfill_events;
DROP TABLE IF EXISTS public.ledger_unpriced_backfill_20260824;

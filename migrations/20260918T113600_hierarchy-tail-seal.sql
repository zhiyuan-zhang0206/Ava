-- Tail seal (task #3981 C, 2026-09-18): the hierarchy worker gains a second
-- job kind, 'tail'. A tail job seals the trailing stretch (the units since
-- the last compact trigger) of an agent that has gone idle, so default
-- run-timeline windows show real layer blocks instead of an uncovered tail;
-- the first build and the day-boundary backstop already seal tails, this
-- makes the seal periodic. Tail cells are provisional: a later rebuild
-- re-cuts them (the write-side reconciliation removes earlier cuts), while
-- compact-sealed cells reproduce identically.
--
-- hierarchy_worker_state.last_tail_seal_cp_id — the tail delta gate (review
-- Q2): a tail seal is due only when the agent's newest checkpoint id exceeds
-- the last sealed one (UUIDv6: lexicographic order is time order). NULL
-- until the agent's first tail seal. Both alters ride one migration so a
-- half-applied state cannot exist (the CHECK would reject the new kind, or
-- the gate would find its column missing).
--
-- The CHECK widen drops-then-adds under IF EXISTS so the body also runs on a
-- fresh bootstrap, where schema.sql already carries the new shape (the
-- migration smoke replays on fresh).

ALTER TABLE hierarchy_jobs
    DROP CONSTRAINT IF EXISTS hierarchy_jobs_kind_check;

ALTER TABLE hierarchy_jobs
    ADD CONSTRAINT hierarchy_jobs_kind_check
    CHECK (kind IN ('compact', 'tail'));

ALTER TABLE hierarchy_worker_state
    ADD COLUMN IF NOT EXISTS last_tail_seal_cp_id TEXT;

COMMENT ON COLUMN hierarchy_worker_state.last_tail_seal_cp_id IS
    'Newest checkpoint id a tail-seal build has sealed for the agent (task '
    '#3981 C): the tail delta gate — null until the first tail seal.';

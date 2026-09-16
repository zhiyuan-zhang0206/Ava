-- Hierarchical understanding nodes (task #3704): the materialized level tree
-- behind the run timeline's narrative layers. One row per sealed node: the
-- identity is the deterministic message span (agent_id, depth, span_start,
-- span_end); the node's single text, plus the two hashes that make writes
-- idempotent (text_hash: identical text = no-op rewrite) and reruns free
-- (input_hash: the generation cache key).
--
-- Span identity is stable: compaction boundaries are never trimmed (#1125),
-- so the stitched full history is append-only and message indices never shift.
--
-- The ava_runner write grant: the generation pass ships as a gateway-side
-- worker, but the operational first-run / ad-hoc regeneration path executes
-- from the agent/runner side (task #3704), so the runner needs the write
-- surface. Nothing deletes rows (lifecycle follows the checkpoint retention,
-- which never deletes boundaries), so DELETE stays out. Gated on the role's
-- existence so the fresh-bootstrap smoke (migration replay on a schema.sql
-- DB, where the role does not exist yet) stays green.
CREATE TABLE understanding_nodes (
    id BIGSERIAL PRIMARY KEY,
    agent_id BIGINT NOT NULL,
    depth INTEGER NOT NULL CHECK (depth >= 1),
    span_start INTEGER NOT NULL,
    span_end INTEGER NOT NULL,
    start_ts TIMESTAMPTZ,
    end_ts TIMESTAMPTZ,
    segment_key TEXT NOT NULL,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    children_count INTEGER NOT NULL,
    parent_id BIGINT REFERENCES understanding_nodes(id) ON DELETE SET NULL,
    model TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    -- The tree is append-only: every written node is final (its text is a pure
    -- function of its input hash). Kept explicit so a future
    -- structure-ahead-of-text pass can record provisional rows; today every
    -- row is TRUE.
    sealed BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One node per deterministic structure identity.
CREATE UNIQUE INDEX understanding_nodes_identity
    ON understanding_nodes (agent_id, depth, span_start, span_end);
-- Window intersection for the run-timeline serving merge.
CREATE INDEX understanding_nodes_window
    ON understanding_nodes (agent_id, start_ts, end_ts);
-- The generation reuse cache lookup (input_hash -> text).
CREATE INDEX understanding_nodes_reuse
    ON understanding_nodes (agent_id, input_hash);

COMMENT ON TABLE understanding_nodes IS
    'Materialized hierarchical understanding nodes (task #3704): one text per '
    'sealed (agent_id, depth, message span); append-only identity, hashes for '
    'idempotent writes and zero-cost reruns.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ava_runner') THEN
        GRANT SELECT, INSERT, UPDATE ON understanding_nodes TO ava_runner;
        GRANT USAGE, SELECT ON SEQUENCE understanding_nodes_id_seq TO ava_runner;
    END IF;
END $$;

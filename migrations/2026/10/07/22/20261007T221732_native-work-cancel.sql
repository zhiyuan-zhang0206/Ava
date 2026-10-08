ALTER TABLE agents_meta ADD COLUMN native_work_id UUID;
CREATE TABLE native_graph_work (
    id UUID PRIMARY KEY,
    agent_id BIGINT NOT NULL CHECK (agent_id > 0),
    machine TEXT NOT NULL CHECK (machine <> ''),
    generation UUID NOT NULL,
    owner UUID NOT NULL,
    protocol SMALLINT NOT NULL CHECK (protocol = 1),
    phase TEXT NOT NULL CHECK (phase IN ('preparing','active','settled','abandoned','uncertain')),
    transfer_chain JSONB NOT NULL DEFAULT '[]',
    settled_checkpoint_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ
);
CREATE TABLE native_cancel_commands (
    id UUID PRIMARY KEY,
    work_id UUID NOT NULL UNIQUE,
    agent_id BIGINT NOT NULL CHECK (agent_id > 0),
    operation_key TEXT NOT NULL UNIQUE,
    request JSONB NOT NULL,
    acceptance JSONB NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'accepted'
        CHECK (outcome IN ('accepted','applied','recovered_stopped','uncertain')),
    checkpoint_id TEXT,
    recovery_checkpoint_id TEXT,
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at TIMESTAMPTZ
);
CREATE INDEX native_cancel_pending_agent_idx ON native_cancel_commands (agent_id, accepted_at, id)
    WHERE outcome IN ('accepted', 'uncertain');

COMMENT ON TABLE native_graph_work IS
    'Native invocation identities and same-transaction certified resource transfers. Retain unsettled work; no inferred closure or TTL.';
COMMENT ON TABLE native_cancel_commands IS
    'Guarded native cancel acceptance and checkpoint/certified-stop evidence. Dedicated domain, never generic inbound; retained without FK or expiry.';

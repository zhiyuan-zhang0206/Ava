CREATE TABLE agent_launch_retry_receipts (
    operation_key text PRIMARY KEY,
    agent_id bigint NOT NULL,
    prior_attempt_id uuid NOT NULL,
    launch_attempt_id uuid NOT NULL UNIQUE,
    machine text NOT NULL,
    config_overlay jsonb,
    birth_config jsonb,
    acceptance jsonb NOT NULL
);
COMMENT ON TABLE agent_launch_retry_receipts IS
'Immutable guarded retry-launch intents; retain after target deletion, no TTL.';

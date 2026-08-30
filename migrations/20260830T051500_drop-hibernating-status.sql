-- Drop the 'hibernating' agent status (2026-08-30: hibernate chain deleted —
-- hosted runner is the end state; see future/infra/agent-runner-as-server.md).

-- Expand-contract, same shape as 20260822T220000 (allocated/starting):
-- fix the rows first (hibernating -> idling — the parked row's external
-- projection was already idling; its pid is stale by construction, so the
-- process-mode reapers / next wake own it from here), then swap the CHECK.
UPDATE agents_meta
SET status = 'idling'
WHERE status = 'hibernating';

ALTER TABLE agents_meta
    DROP CONSTRAINT agents_meta_status_check;

ALTER TABLE agents_meta
    ADD CONSTRAINT agents_meta_status_check
    CHECK (status IN ('running', 'idling', 'restarting', 'terminated'));

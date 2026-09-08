-- Shell TTL renewal (task #2647): ava.shell.sessions.renew(id, ttl=) lets the
-- owning agent extend a live session's deadline explicitly, before it passes.
-- `renewals` / `last_renewed_at` on the main row are the cheap display facts;
-- `agent_shell_ttl_renewals` is the append-only audit trail (one row per
-- renewal, written in the same transaction as the deadline UPDATE). Expired
-- rows are never renewable: the renewal UPDATE guards expires_at > now(), and
-- the reaper re-checks the same predicate before dispatching a kill (user
-- ruling 2026-09-08: TTL expiry = immediate reclamation, no renewable
-- expired state).
ALTER TABLE agent_shell_ttls
    ADD COLUMN renewals INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN last_renewed_at TIMESTAMPTZ;

CREATE TABLE agent_shell_ttl_renewals (
    id                    BIGSERIAL PRIMARY KEY,
    agent_id              BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    session_id            BIGINT NOT NULL,
    requested_ttl_seconds DOUBLE PRECISION NOT NULL,
    prev_expires_at       TIMESTAMPTZ NOT NULL,
    new_expires_at        TIMESTAMPTZ NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-session history, oldest first: the trail behind the monitor page's
-- "renewed N×" count and the auditor's before/after look.
CREATE INDEX agent_shell_ttl_renewals_agent_session_idx
    ON agent_shell_ttl_renewals (agent_id, session_id, created_at, id);

COMMENT ON TABLE agent_shell_ttl_renewals IS
    'Append-only shell-TTL renewal trail: one row per ava.shell.sessions.renew call, in the same transaction as the deadline UPDATE. prev/new_expires_at carry the before/after deadlines. No FK to agent_shell_ttls — the reaper deletes that row on reclamation while the audit history must survive.';

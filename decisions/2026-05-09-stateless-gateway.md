# Stateless, restartable gateway

## Context

The gateway is the spawner of agents — it owns the HTTP control plane (spawn / resurrect / terminate / send-message). Originally it was excluded from self-rolling upgrades on the premise that "the thing that restarts agents is a different class of problem and can't restart itself." That premise needed testing: if the gateway could be restarted at any time without corrupting state, it stops being a special case and folds into the normal upgrade path.

Measurement removed the first objection. Cold restart is ~0.6s end-to-end (dominated by Python import; the DB pool rebuild is ~0.01s), well under the SDK's 5s call timeout. The gateway holds almost no irreplaceable state: agent lifecycle, threads, inbound queue, and schedules all live in Postgres; agent processes live in independent OS sessions that don't depend on the gateway being alive; SSE clients reconnect on their own. The only memory-resident state — DB pool, SSE sockets, app state — is cheap to rebuild.

Two things stood between that and "restart at any time":

1. **A committed-but-incomplete window.** spawn / resurrect / respawn commit a DB row in status `allocated`, then launch the OS session. A restart landing in that gap leaves an orphan `allocated` row with no process behind it.
2. **An unowned piece of process state.** The SDK manages persistent interactive shells directly via session names of the form `thread_{id}_{name}`. The gateway was unaware they existed (no endpoint, no table). For a stateless gateway, shell discovery must not depend on gateway process memory.

## Decision

Make the gateway a stateless, cold-restartable process. Recover everything it needs by reading the durable substrate on startup; never reconstruct from in-process memory.

- **SDK retries network-layer failures.** Gateway calls retry on transport errors (connect/timeout/protocol) — 3 attempts, 1s apart — covering the ~0.6s restart window. HTTP 4xx/5xx are not retried (application errors don't get better by repeating). The retry count is preserved in the final error so a real outage is distinguishable from a transient one.
- **The session server is the source of truth for shells.** Shell state already lives in the session server (pane buffers, running commands, cwd). The gateway keeps no shell registry; it parses the session list on demand. Session names become purely structural — `ava-{agent_id}-shell-{shell_id}`, monotonic `shell_id` per agent — so attribution is an O(1) regex match with no DB lookup and no name-escaping. A new `GET /api/agents/{id}/shells` endpoint runs a session listing and parses live on every request; if the server is briefly unreachable it returns empty rather than 500.
- **A watcher reaps orphan `allocated` rows.** The already-independent restart watcher, polling, promotes or terminates `allocated` rows that have outlived the launch window. This closes the commit→launch gap as a fallback without changing spawn/resurrect semantics.

Together these let the gateway enter the normal self-rolling upgrade path: terminate agents, restart the gateway, continue — orphans from the restart window are swept by the watcher.

## Alternatives rejected

**Shell registry in Postgres** (a `shell_sessions` table; SDK calls the gateway to register/unregister on new/kill). Rejected: it creates two sources of truth that drift, since the session server is the real state and the table only mirrors it. It also puts an HTTP round-trip on the hot path of every shell create/destroy, and solves a problem that doesn't exist — the server is already reliable, restart-surviving state storage.

**Shell metadata in server environment variables** (`set-environment`). Rejected: the session name alone already encodes everything needed (agent_id + shell_id), and reading env vars means extra server calls and more parsing than reading a structured name directly.

**Encoding the human-readable shell name into the session name.** Rejected: arbitrary names force escaping and turn gateway attribution into fragile parsing. The name is demoted to an optional label the agent tracks itself; the session name stays strictly structural.

**Two-phase commit or startup replay to close the launch window.** Both work, but the watcher fallback was preferred: it adds a recovery layer without touching spawn/resurrect control flow, and reuses an existing always-on poller instead of introducing replay logic into gateway startup or rewriting each write path.

**A migration for the shell-naming change.** Rejected: orphaned old-format sessions are invisible to the new code and die out naturally when the session server restarts. Migration code buys too little to justify its complexity. No fallback compatibility mode is kept either — the system does not maintain two naming schemes.

## Consequences

- The gateway is restartable at any time and is no longer excluded from self-rolling upgrades. Recovery is read-from-substrate, not state transfer: a fresh gateway re-derives the full picture by querying Postgres and re-listing sessions.
- Restart is cold only — there is no hot reload; a new process replaces the old one.
- Agents do not crash during a gateway restart unless they happen to be mid-call to a gateway endpoint; such a call surfaces as a normal turn error fed back to the model, after retries are exhausted.
- Concurrent shell creation can collide on a `shell_id` (both probe the same max); the loser of the `new-session` race retries with the next id. This is the cost of having no central allocator.
- Shells survive agent termination and become visible again on resurrect — treated as a feature (the terminal wasn't closed), with the watcher detecting but never auto-killing orphans, since auto-kill races a resurrection in progress.
- If the session server itself dies, all sessions are lost and shell ids restart from zero; this is accepted as equivalent to closing a terminal window, and the agent processes are re-spawned by the watcher anyway.
- The commit→launch orphan window is not eliminated at the source; it is covered by a sweep. The trade accepted is eventual cleanup over strict atomicity, in exchange for leaving spawn/resurrect semantics untouched.

---

*Forward link (2026-08-13): the "if the session server itself dies, all
sessions are lost" bound was retired for agent shells —
[per-session pty hosts](2026-08-13-per-session-pty-hosts.md) moved each
session into its own detached host process, so a dying backend now takes
down exactly one session, and cluster updates take down none.*

Forward link (2026-08-22): the launch-orphan state is now unclaimed idling; see
[agent status model](../docs/history/2026-08-22/agent-status-model.md).

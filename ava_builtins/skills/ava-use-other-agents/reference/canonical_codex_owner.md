# Canonical Codex ownership

Read this reference before launching Codex. `spawn_codex.py` owns one active
generation keyed by `(resolved cluster home, resolved workspace path, codex)`.
A workspace basename is only a display label. Concurrent callers have one
winner; later callers, including another Ava agent, wait for and adopt the live
record instead of stacking another Codex process.

An expired, crashed, or terminated-owner record — and a supervised record whose
supervisor died — transfers to a fresh generation only after the old Codex PTY
and private state are reclaimed; a live takeover record is instead adopted
(its coding session is its whole liveness). After PTY allocation, the launcher
publishes its active handle before waiting for Codex startup and bootstrap. A
launching record that exceeds its spawn grace remains busy while a matching PTY
is live.
Each successful command prints the core file handles plus these Codex fields:

```text
adopted=<true|false>
status=<launching|active|terminal>
generation=<opaque generation>
owner_agent_id=<id>
session_id=<id>
session_name=<full PTY name>
supervisor_session_id=<id>
supervisor_session_name=<full PTY name>
codex_home=<generation-private path>
tasks_file=<absolute path>
work_file=<absolute path>
```

A takeover generation (`--impersonate-self`) prints no `supervisor_*`,
`tasks_file`, or `work_file` line: it runs file- and supervisor-less, its
briefing inlined in the launch message, and its coding session alone is its
liveness signal.

The numeric ids remain scoped to `owner_agent_id`. A different Ava agent that
adopts the record must not pass those ids to its own `ava.shell.sessions`
methods; it should use the canonical status/cancel commands or coordinate with
the recorded owner. Full names are the host identities used by lifecycle
cleanup.

Every generation receives a private `CODEX_HOME`, seeded only with `auth.json`
and a configuration snapshot containing the workspace trust row. No SQLite
database, mutable log, transcript, or resume state crosses generations. A
fresh supervised worker rebuilds from the task file, work log, collaboration
contract, and Git tree; a takeover process has none of those files and
rebuilds from the briefing inlined in its launch message instead.

Every takeover generation also owns the explicit shared app server the relay
delivers into: the endpoint must belong to the server holding the existing
conversation; Pending queue acceptance does not establish Steer delivery. The launcher starts `codex app-server --listen unix://<socket>`
on a private per-generation socket under the cluster's `run/` directory (with
`approval_policy="never"` and `sandbox_mode="danger-full-access"` configured on
the server itself), starts a janitor that ends the server when the coding
session dies, connects the TUI with `--remote` to that exact endpoint, and
carries the endpoint into the launch message so the request records it
(`--codex-remote`). This needs a codex release with `app-server --listen`, TUI
`--remote`, and app-server start-or-steer support (verified on 0.155.1).
Missing host capabilities fail visibly, and a takeover additionally prints
`codex_app_server=<endpoint>`.

Cleanup boundary: the janitor is the only cleanup owner — if it never starts,
or is itself killed (for example with the whole session tree), an orphan app
server and socket can remain; reap them by hand via the printed
`codex_app_server=<endpoint>`. The normal stop paths (session death, expiry)
leave no residue.

The default TTL is four hours and can be adapted with `--ttl-seconds` up to the
Persistent Shell one-day maximum. TTL is a crash backstop. The automatically
started supervisor closes and terminalizes the exact generation on current
`DONE` or final `HANDOFF`, explicit cancel, owner termination, Codex death,
stalled launch, work-file deletion, or expiry. Its notifications never
resurrect a terminated owner. A takeover generation starts no supervisor:
explicit cancel and expiry are its stop paths, and the next launch reclaims a
dead takeover record before rebuilding.

Inspect or cancel with the exact printed generation:

```bash
.venv/bin/python reference/spawn_codex.py <workspace-dir> --status
.venv/bin/python reference/spawn_codex.py <workspace-dir> \
  --cancel-generation <generation>
```

A stale cancel token cannot stop a replacement. For a full handoff, let the old
generation reach `HANDOFF`, launch the same workspace again, and use the newly
printed owner and handles. Never resume the old Codex SQLite session or reuse
its numeric PTY id.

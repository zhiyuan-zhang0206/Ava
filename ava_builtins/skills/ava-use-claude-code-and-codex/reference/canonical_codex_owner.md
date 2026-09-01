# Canonical Codex ownership

Read this reference before launching Codex. `spawn_codex.py` owns one active
generation keyed by `(resolved cluster home, resolved workspace path, codex)`.
A workspace basename is only a display label. Concurrent callers have one
winner; later callers, including another Ava agent, wait for and adopt the live
record instead of stacking another Codex process.

An expired, crashed, unsupervised, or terminated-owner record transfers to a
fresh generation only after the old Codex PTY and private state are reclaimed.
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

The numeric ids remain scoped to `owner_agent_id`. A different Ava agent that
adopts the record must not pass those ids to its own `ava.shell.sessions`
methods; it should use the canonical status/cancel commands or coordinate with
the recorded owner. Full names are the host identities used by lifecycle
cleanup.

Every generation receives a private `CODEX_HOME`, seeded only with `auth.json`
and a configuration snapshot containing the workspace trust row. No SQLite
database, mutable log, transcript, or resume state crosses generations. A
fresh worker rebuilds from the task file, work log, collaboration contract, and
Git tree.

The default TTL is four hours and can be adapted with `--ttl-seconds` up to the
Persistent Shell one-day maximum. TTL is a crash backstop. The automatically
started supervisor closes and terminalizes the exact generation on current
`DONE` or final `HANDOFF`, explicit cancel, owner termination, Codex death,
stalled launch, work-file deletion, or expiry. Its notifications never
resurrect a terminated owner.

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

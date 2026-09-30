# Canonical Codex ownership

Read this reference before launching Codex. Every `spawn_codex.py` launch owns
a generation of its own under `(resolved cluster home, resolved workspace path,
codex)`. Several can share a workspace, and a workspace basename is only a
display label.

Before creating its own generation, a launch reclaims the dead ones in that
workspace. It stops their PTYs and removes their private state:
- a generation that expired, crashed, or whose owner agent was terminated;
- a launch that stalled past its spawn grace with no live PTY;
- a supervised generation whose supervisor died (nobody would close it on `DONE`).

Live generations are left alone, takeovers included (a takeover's coding session
is its whole liveness). After PTY allocation, the launcher publishes its active
handle before waiting for Codex startup and bootstrap.
Each successful command prints the core file handles plus these Codex fields:

```text
status=<launching|active|terminal>
generation=<opaque generation>
owner_agent_id=<id>
session_id=<id>
session_name=<full PTY name>
supervisor_session_id=<id>
supervisor_session_name=<full PTY name>
tasks_file=<absolute path>
work_file=<absolute path>
codex_session=<uuid>
```

`codex_session` is Codex's own session id, read from its `/status` card: the id
`--resume` takes after an interruption.

A takeover generation (`--impersonate-self`) prints no `supervisor_*`,
`tasks_file`, or `work_file` line: it runs file- and supervisor-less, its
briefing inlined in the launch message, and its coding session alone is its
liveness signal.

The numeric ids remain scoped to `owner_agent_id`. Another Ava agent must not
pass those ids to its own `ava.shell.sessions` methods; it should use the
status/cancel commands or coordinate with the recorded owner. Full names are
the host identities used by lifecycle cleanup.

Codex runs on the host user's own `~/.codex`, exactly as in a person's own
terminal, so its conversation outlives the shell. Nothing is written to that
home's configuration: each launch passes per-session `-c` overrides that trust
the workspace and turn off the startup update check (an unattended launch must
never answer "update now"). A fresh generation starts a new Codex session;
`--resume <codex_session>` reopens a recorded one instead — see [Resume after
an interruption](resume_after_interruption.md). The generation's private state
directory holds only a takeover's app-server log.

Every takeover generation also owns the explicit shared app server the relay
delivers into: the endpoint must belong to the server holding the existing
conversation; Pending queue acceptance does not establish Steer delivery. The
launcher starts `codex app-server --listen unix://<socket>` on a private
per-generation socket (with `approval_policy="never"` and
`sandbox_mode="danger-full-access"` configured on the server itself), starts a
janitor that ends the server when the coding session dies, connects the TUI with
`--remote` to that exact endpoint, and carries the endpoint into the launch
message so the request records it (`--codex-remote`). This needs a codex release
with `app-server --listen`, TUI `--remote`, and app-server start-or-steer
support (verified on 0.155.1). Missing host capabilities fail visibly, and a
takeover additionally prints `codex_app_server=<endpoint>`.

The socket sits in a short per-user directory, not under the cluster home:
`/private/tmp/ava-<uid>` on macOS and `/tmp/ava-<uid>` elsewhere. The directory
is created 0700 and refused unless it is this user's real directory. A long
cluster home therefore cannot
push the path past the unix-socket limit (`sun_path`); a path that still would
not fit fails before launch instead of timing out.

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
explicit cancel and expiry are its stop paths, and the next launch in the
workspace reclaims a dead takeover record.

`--status` prints every generation recorded for the workspace (blank-line
separated). Cancel exactly one of them by its printed generation:

```bash
.venv/bin/python scripts/spawn_codex.py <workspace-dir> --status
.venv/bin/python scripts/spawn_codex.py <workspace-dir> \
  --cancel-generation <generation>
```

A cancel names exactly one generation and never touches another. For a full handoff, let the old
generation reach `HANDOFF`, launch the same workspace again, and use the newly
printed owner and handles; never reuse the old numeric PTY id. A handoff starts
a fresh session on purpose — resume is for an interruption, not a handoff.

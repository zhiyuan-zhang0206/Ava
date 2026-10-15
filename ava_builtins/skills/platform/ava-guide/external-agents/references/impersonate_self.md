# Let the coding agent take over your identity

Use this mode when an external coding agent should replace you, address the
human through your normal Ava chat, and call Ava capabilities as you. A takeover
is **file-less**: spawn the coding agent normally and pass the takeover prompt
inline with `--prompt` — no task file, no work file, no watcher; the process
reads nothing from the workspace and writes nothing back into it. Run the spawn
script in your Ava execution context:

```bash
.venv/bin/python scripts/spawn_codex.py <workspace-dir> \
  --prompt '<briefing + instructions to read impersonator-guide and run ava impersonate request>'
```

The `--prompt` text is sent as the initial message the new session wakes up
with: include your agent id (`ava.self.AGENT_ID`), the current goal, decisions,
constraints, paths, and instructions to read `impersonator-guide` and run
`ava impersonate request --agent <agent_id> --provider <provider> ...`.
The script's startup checks — readiness, message submission, the generation's
owner record — run before it returns; for Codex it also starts the shared app
server the session and the relay both use (`codex_app_server=<endpoint>`, so
the executor can pass `--codex-remote <endpoint>` or rely on the default). On
the next safe boundary Ava saves your checkpoint, verifies the inbound relay
and activates the replacement; the executor may freely choose its display name.
Activation and relay liveness are not host receipt: once active, the takeover
verifies the start message actually arrived in its conversation before relying
on push delivery. Run it from your own workspace — usually the best choice for
a takeover: spawn the session under your workspace and pass that directory as
the spawn workspace argument, so the workspace is directly the impersonator's
working directory (other locations are not forbidden; this is the recommended
default).

All four launchers accept `--prompt`:
- `spawn_codex.py <workspace-dir> --prompt '...'` — starts the shared app server
  (`codex_app_server=<endpoint>`) and launches Codex connected with `--remote`.
- `spawn_claude.py <workspace-dir> --prompt '...'` — launches Claude Code and
  sends the prompt; the executor requests the lease with `--provider claude` and
  arms its `Monitor` watch.
- `spawn_antigravity.py <workspace-dir> --prompt '...'` — launches `agy chat -a <workspace>`
  and sends the prompt; the executor requests the lease with `--provider antigravity`
  (the runtime starts and supervises the ConnectRPC relay).
- `spawn_dsh.py <workspace-dir> --prompt '...'` — boots dsh's `headless` profile
  with the bundled `ava-relay-dsh` plugin, which opens one persistent session
  with the prompt and starts the relay from the stub the request writes.

The two states, and nothing else:

- **Start — the process interrupts you.** The platform saves a checkpoint and
  your execution pauses when the takeover activates. You and the replacement
  never run at the same time; there is no file lockstep and nothing to
  supervise.
- **End — one message resumes you.** The takeover releases with its own
  summary; a system note resumes you carrying that summary and the path of
  `impersonation/<session_id>.json`. Read that file before acting on pending
  human input: it retains all messages (unACKed and acknowledged alike),
  operations and consumed events — and an ACK means received, never finished,
  so check acknowledged messages for work the summary does not show as done.

If the takeover's shell is closed (a full stop, a reboot, a crash), the lease
expires at its TTL or when ended, and resumes you with the handoff JSON. To
continue with the same executor, relaunch with
`--resume <claude_session|codex_session|antigravity_session> --prompt '...'`: it
reopens the executor's own conversation, and the prompt tells it which session
ended and where the handoff JSON is; it then opens a new impersonation session,
since the old one is over. See [Resume after an
interruption](resume_after_interruption.md). A DeepSeek Harness takeover is
relaunched fresh instead.

TTL remains the recovery deadline if the takeover dies. Without `--prompt` the
normal delegated workflow applies and you remain the supervisor making
decisions.


### Delivery safety

The launch message is long, and a raw composer burst can be folded by the
TUI into content-dropping paste fragments (#4364). The launcher wraps it in
bracketed-paste markers so the composer takes it as one atomic paste. If a
launch still reports `start-receipt=not-submitted`, fall back to the
file-driven path: write the briefing to a file in the workspace, send the
session a short pointer message naming that file, and let the executor read
the brief from disk before acting.

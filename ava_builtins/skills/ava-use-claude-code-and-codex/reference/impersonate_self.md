# Let the coding agent take over your identity

Use this mode when Codex should replace you, address the human through your
normal Ava chat, and call Ava capabilities as you. A takeover is **file-less**:
write the briefing as text and pass it inline with `--brief` — no task file, no
work file, no watcher; the process reads nothing from the workspace and writes
nothing back into it. Run the spawn script in your Ava execution context:

```bash
.venv/bin/python reference/spawn_codex.py <workspace-dir> --impersonate-self \
  --impersonation-name 'Fix login' --brief '<the full briefing text>'
```

The briefing is inlined verbatim into the launch message the new process wakes
up with: include the current goal, decisions, constraints and paths. The
script's startup checks — readiness, message submission, the generation's owner
record — run before it returns; for Codex it also starts the shared app server
the takeover and the relay both use, with the endpoint carried in the launch
message. On the next safe boundary Ava saves your checkpoint, verifies the
inbound relay and activates the replacement; the executor may freely choose its
display name. Activation and relay liveness are not host receipt: once active,
the takeover verifies the start message actually arrived in its conversation
before relying on push delivery. Run it from your own workspace —
usually the best choice for a takeover: spawn the takeover under your
workspace and pass that directory as the spawn workspace argument, so the
workspace is directly the impersonator's working directory (other locations
are not forbidden; this is the recommended default). A workspace that already
carries a live canonical generation is refused (`--cancel-generation` it
first). Both launch paths are live: `spawn_claude.py --impersonate-self` takes
the same inline `--brief`; its relay starts automatically with the session (the
bundled `ava-relay` plugin, resident mode — no `timeout_ms` arming; the request
output names the credential stub and the manual fallback). `--no-relay-resident`
restores the executor-armed flow.

The two states, and nothing else:

- **Start — the process interrupts you.** The platform saves a checkpoint and
  your execution pauses when the takeover activates. You and the replacement
  never run at the same time; there is no file lockstep and nothing to
  supervise.
- **End — one message resumes you.** The takeover releases with its own
  summary; a system note resumes you carrying that summary and the path of
  `impersonation/<session_id>.json`. Read that file before acting on pending
  human input: it retains all messages (including unACKed ones), operations and
  consumed events.

TTL remains the recovery deadline if the takeover dies. Without
`--impersonate-self` the normal delegated workflow applies and you remain the
supervisor making decisions.


### Delivery safety

The launch message is long, and a raw composer burst can be folded by the
TUI into content-dropping paste fragments (#4364). The launcher wraps it in
bracketed-paste markers so the composer takes it as one atomic paste. If a
launch still reports `start-receipt=not-submitted`, fall back to the
file-driven path: write the briefing to a file in the workspace, send the
session a short pointer message naming that file, and let the executor read
the brief from disk before acting.

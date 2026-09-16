# Let the coding agent take over your identity

> **Suspended:** takeover execution is suspended pending fixes; it resumes with the fix line. The steps below describe the target flow — do not launch takeovers until the suspension is lifted.

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
record — run before it returns. On the next safe boundary Ava saves your
checkpoint, verifies the inbound relay and activates the replacement; the
executor may freely choose its display name. Run it from your own workspace —
usually the best choice for a takeover: spawn the takeover under your
workspace and pass that directory as the spawn workspace argument, so the
workspace is directly the impersonator's working directory (other locations
are not forbidden; this is the recommended default). A workspace that already
carries a live canonical generation is refused (`--cancel-generation` it
first). Only the Codex path is live — Claude Code's takeover launch path is
still being reworked (task #3688), and `spawn_claude.py --impersonate-self`
refuses to run.

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

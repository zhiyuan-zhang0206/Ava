# Let the coding agent take over your identity

> **Suspended:** takeover execution is suspended pending fixes; it resumes with the fix line. The steps below describe the target flow — do not launch takeovers until the suspension is lifted.

Use this mode when Codex or Claude Code should replace you, address the human
through your normal Ava chat, and call Ava capabilities as you. Write a concrete
brief in the task file before launching, including the current goal, decisions,
constraints and paths. Run the spawn script in your Ava execution context:

```bash
.venv/bin/python reference/spawn_codex.py <workspace-dir> --impersonate-self --impersonation-name 'Fix login'
.venv/bin/python reference/spawn_claude.py <workspace-dir> --impersonate-self --impersonation-name 'Fix login'
```

Each script instructs the new process to request a named session for your agent
id and read the impersonator guide. The executor may freely choose its display
name. A running canonical Codex worker cannot be adopted into this mode; launch
with its own workspace. Without this flag the normal delegated workflow below
applies and you remain the supervisor making decisions.

Complete the usual startup and supervision checks before returning from your
launching call. On the next safe boundary Ava saves your checkpoint, verifies
the inbound relay and activates the replacement automatically. You then pause;
normal user messages go to the controller. The controller uses `ava impersonate say`
for human progress/questions and ACKs handled input. It keeps the work file at
`STATUS: WORKING` for process supervision until it releases with its own summary.
Only after release may it report `STATUS: DONE` and exit. TTL remains the recovery
deadline if it dies. Upon return, your first new system note contains its summary
and one JSON path, `impersonation/<session_id>.json`. Read that file before acting
on pending human input. It retains all messages, operations and consumed events.

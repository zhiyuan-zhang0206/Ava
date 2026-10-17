# Claude Code as the impersonator

Host-specific half of the [impersonator guide](../SKILL.md): how the relay
reaches a Claude Code session and the traps seen in practice. Everything else —
inheriting the borrowed context, messages, ACK, renewal, SDK, release — is in
the general guide.

## Relay startup

Immediately after `meiva impersonate request --provider claude`, start the relay
as a `Monitor` watch — the activation gate waits for its heartbeat:

```json
{
  "command": "MEIVA_IMPERSONATION_RELAY_TOKEN=<relay token> <checkout>/.venv/bin/meiva impersonate relay <agent_id> --session <session_id> --provider claude",
  "description": "Meiva agent <agent_id> inbox",
  "timeout_ms": 1800000
}
```

A watch is capped at 30 minutes. At the deadline Claude Code kills it together
with the relay and posts one `Monitor expired … Re-arm it` notice: re-arm at
once with the same command. A missed re-arm visibly degrades delivery; it
does not end executor authority before the original TTL.

## How messages arrive

Each relay line becomes a notification in your conversation: one envelope per
batch with the full content, the message ids, and the exact ACK command. A
notification can arrive while you are mid-task: acknowledge it as soon as it
reaches you — the ACK is receipt, not completion — then fit its work into your
run. Nothing needs polling.

## Authority

Control commands are authorized by the `(agent_id, session_id)` lease handle on
the hosting machine. Run them from your own Bash tool or from a subagent inside
this session, preserving `MEIVA_HOME`.

## Traps

- **Signed-out CLI.** The standalone CLI keeps its own login, separate from a
  desktop app's. A signed-out CLI still renders its panel, then answers the
  first message with `Not logged in · Please run /login`; the launcher's
  `claude auth status` preflight refuses such a launch.
- **Cancel.** A `cancel` is acknowledged on receipt like every message — then
  stop: use your own interrupt for an in-flight tool. The ACK never substitutes
  for stopping.
- **Permissions.** A takeover runs unattended under
  `--dangerously-skip-permissions`; nobody answers approval prompts.

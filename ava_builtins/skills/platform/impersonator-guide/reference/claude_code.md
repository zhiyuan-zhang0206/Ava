# Claude Code as the impersonator

Host-specific half of the [impersonator guide](../SKILL.md): how the relay
reaches a Claude Code session, where your authority comes from, and the traps
seen in practice. Everything else — inheriting the borrowed context, messages,
ACK, renewal, SDK, release — is in the general guide.

## Relay startup

Your launch message says which of two flows applies.

**Resident (the default for `spawn_claude.py --impersonate-self`).** The
session was started with the bundled `ava-relay` plugin. Run the request from
your Bash tool; it writes the scoped relay credential to the stub named in
`AVA_IMPERSONATION_RELAY_STUB` (inside the launch generation's private state
directory) and does not print it. The plugin's session-lifetime monitor
consumes the stub once and runs the relay for the rest of the session. Arm no
Monitor watch yourself. If the request output reports the stub unconsumed and
no heartbeat starts, follow the manual flow it describes instead.

One resident relay serves one request: a second `ava impersonate request` from
the same session is refused before any lease exists. To take over another
agent, finish or cancel this takeover and launch a fresh session.

**Manual (launched with `--no-relay-resident`, or a session nobody launched).**
Immediately after the request, start the relay as a `Monitor` watch — the
activation gate waits for its heartbeat:

```json
{
  "command": "AVA_IMPERSONATION_RELAY_TOKEN=<relay token> <checkout>/.venv/bin/ava impersonate relay <agent_id> --session <session_id> --provider claude",
  "description": "Ava agent <agent_id> inbox",
  "timeout_ms": 1800000
}
```

A watch is capped at 30 minutes. At the deadline Claude Code kills it together
with the relay and posts one `Monitor expired … Re-arm it` notice: re-arm at
once with the same command. A missed re-arm visibly degrades delivery; it
does not end executor authority before the original TTL or confirmed death.

## How messages arrive

Each relay line becomes a notification in your conversation: one envelope per
batch with the full content, the message ids, and the exact ACK command. A
notification can arrive while you are mid-task: acknowledge it as soon as it
reaches you — the ACK is receipt, not completion — then fit its work into your
run. Nothing needs polling.

## Authority

Control commands are attested against the `claude` process that made the
request (a native install's version-named binary counts). Run them from your
own Bash tool or from a subagent inside this session; both descend from that
process. A command started from another terminal, a detached helper, or deeper
than 8 process levels is refused.

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

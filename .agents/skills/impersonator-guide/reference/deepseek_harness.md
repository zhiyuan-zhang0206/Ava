# DeepSeek Harness (dsh) as the impersonator

Host-specific half of the [impersonator guide](../SKILL.md): how the relay
reaches a dsh session, where your authority comes from, and the traps seen in
practice. Everything else — inheriting the borrowed context, messages, ACK,
renewal, SDK, release — is in the general guide.

## Relay startup

There is nothing for you to arm. The Ava relay plugin (`ava-relay.mjs`) runs
inside your dsh process and exports `DSH_AVA_RELAY_STUB` to every shell command
you run. Run the request from your shell tool:

- the request writes the scoped relay credential and the checkout's interpreter
  to that stub (0600) and does not print them;
- the plugin consumes the stub once and runs `ava impersonate relay --provider
  dsh` as a background job owned by your session.

If the request refuses because `DSH_AVA_RELAY_STUB` is unset, the plugin is not
loaded in this session: stop and report that instead of retrying. A relay that
never heartbeats rejects the takeover.

## How messages arrive

The plugin steers each pushed batch into your session: an idle session starts
a turn, and a busy one receives it at its next step. Each batch is one envelope
with the full content, the message ids, and the exact ACK command.

## Authority

Control commands are attested against the `node` process running dsh (its
script is the `dsh` launcher). Your shell tool's commands are its direct
children, so run every control command from there. A command from another
terminal or a detached helper is refused.

## Traps

- **The relay job is load-bearing.** It shows up in `job_list`; never
  `job_kill` it. Killing it stops the heartbeat, and the Ava side ends the
  takeover (`aborted: the bound relay stopped heartbeating`).
- **Keep credentials out of the conversation.** dsh uploads session logs with
  its model requests by default. The request keeps the relay credential in the
  stub for this reason — never print the stub or paste a credential into a
  message.
- **Permission presets.** A self-takeover launched by `spawn_dsh.py` runs with
  `DSH_PERMISSION_MODE=danger-full-access`, since nobody answers approval
  prompts in its PTY. An operator session under the default
  `workspace-write` preset can run the control commands but not work that
  writes outside the workspace.

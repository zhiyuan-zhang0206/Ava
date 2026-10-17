# Antigravity as the impersonator

Host-specific half of the [impersonator guide](../SKILL.md): how the relay
reaches an Antigravity (`agy`) session and the traps seen in practice.
Everything else — inheriting the borrowed context, messages, ACK, renewal, SDK,
release — is in the general guide.

## Relay startup

There is nothing for you to arm. The request records your conversation's cascade
ID, and the accepting Meiva runtime starts and supervises the relay itself at
activation:

- `--provider antigravity` resolves `--thread-id` from the explicit flag or
  from `ANTIGRAVITY_CONVERSATION_ID` in your environment;
- the CLI verifies that the local Antigravity `language_server_macos_*` ConnectRPC
  endpoint (`SendUserCascadeMessage`) is reachable on `127.0.0.1` before
  recording the lease.

If the relay cannot start or the language server is unreachable, acceptance is
rolled back and the lease ends `rejected` with the reason.

## How messages arrive

The relay delivers each batch into your active cascade through the local
Antigravity language server's `SendUserCascadeMessage` RPC: an idle cascade
starts a turn, and a running cascade receives the batch as queued user input.
Each batch is one envelope with the full content, the message ids, and the
exact ACK command. Server acceptance is not host receipt — only your own ACK
records that a message reached you.

## Authority

Control commands are authorized by the `(agent_id, session_id)` lease handle on
the hosting machine. Run them from your `run_command` tool in the same `MEIVA_HOME`
environment.

## Traps

- **Language server lifecycle.** Closing the Antigravity app or CLI session stops
  its local `language_server_macos_*` process, which terminates relay delivery.
  Release the lease with a summary before closing the host session.
- **No fallback transport.** If pushes stop arriving, read the inbox with
  `meiva impersonate inbox <session_id> --agent <agent_id>` as a fallback and note
  it in your release summary.

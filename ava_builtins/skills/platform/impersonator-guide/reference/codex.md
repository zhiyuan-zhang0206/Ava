# Codex as the impersonator

Host-specific half of the [impersonator guide](../SKILL.md): how the relay
reaches a Codex session, where your authority comes from, and the traps seen in
practice. Everything else — inheriting the borrowed context, messages, ACK,
renewal, SDK, release — is in the general guide.

## Relay startup

There is nothing for you to arm. The request records where your conversation
lives, and the accepting Ava runtime starts the relay itself at activation:

- pass `--thread-id` with this session's `CODEX_THREAD_ID`;
- pass `--codex-remote` with the app-server endpoint your launch message names
  (a self-takeover launcher starts one private app server that both your TUI
  and the relay use); without it the CLI resolves the default local daemon
  socket under `$CODEX_HOME`;
- keep `CODEX_HOME` as it is when you run the request.

If the relay cannot start, the acceptance is rolled back and the lease ends
`rejected` with the reason.

## How messages arrive

The relay delivers through the app server's `turn/start`: an idle thread starts
a new turn, and a running turn receives the batch as additional input (Steer).
Each batch is one envelope with the full content, the message ids, and the
exact ACK command. Server acceptance is not host receipt — only your own ACK
records that a message reached you.

## Authority

Control commands are attested against the `codex` process that made the
request. Run them from your own exec tool; that process tree is the authority.
A command from another terminal, a detached helper, or deeper than 8 process
levels is refused.

## Traps

- **Refused input.** Review mode and manual compaction can refuse input. A
  refused delivery stops the relay; the message stays unacknowledged and
  counts against its delivery budget. Avoid both while a lease is active.
- **Wrong endpoint.** A socket's existence does not prove it owns your thread:
  the TUI and the relay must share one app server, the one your launch message
  names.
- **No fallback transport.** The relay never downgrades to Pending delivery or
  resubmits through a second path; if pushes stop arriving, read the inbox as
  a fallback and say so in your release summary.

# Heartbeat pause reminder removal

## Decision

`ava.self.pause_heartbeat` records every pause in the append-only trail,
updates the active heartbeat window, and emits telemetry. It no longer compares
the new duration with the previous row or emits a backoff reminder. Repeating
the 24-hour cap is legal.

`NoteTag.HEARTBEAT_PAUSE` and its timeline dispatch stay intact so historical
checkpoint rows continue to render as ordinary system notes. New SDK
`send_system_note` calls validate their tag against the closed `NoteTag`
vocabulary before the gateway boundary, and a static audit keeps framework
note writers on that vocabulary.

## Consequences

- Repeated or shorter pause windows no longer add an in-exec message.
- The pause trail remains available for inspection without affecting future
  pause calls.
- An unrecognized new system-note tag fails locally instead of reaching the
  frontend's fail-loud unknown-marker path.

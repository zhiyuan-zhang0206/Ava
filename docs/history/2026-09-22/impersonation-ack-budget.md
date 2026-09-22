# Bound impersonation delivery when the executor does not ACK

The user requested termination after two unacknowledged deliveries. We chose
an initial attempt and one retry, each with a full five-minute ACK window.
The budget is per message: unrelated progress does not forgive an ignored row.

An in-memory counter was rejected because relay restart or credential rotation
would reset it. Recording after submission was rejected because a host can
accept a message just before the relay crashes. We reserve before submission,
so an ambiguous send consumes an attempt while preserving the message body.
This bounds duplicates without claiming exactly-once host execution.

Budget exhaustion reuses terminal expiry and the native checkpoint/handoff
protocol. It records the missing-ACK cause without fabricating an ACK or
terminating the native agent. Reads and native reconciliation discover it on
the existing catchup cadence. Empty inboxes do not end impersonation.

Existing rows start at zero on upgrade: historical pushes were not durably
counted, so reconstructing them would be guesswork. Rollback refuses while a
live lease has reserved attempts, preventing a rollback/re-upgrade budget reset.

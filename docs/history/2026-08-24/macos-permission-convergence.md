# macOS permission convergence and signing direction

## Context

macOS TCC prompts recurred because access was attributed to a versioned uv Python
interpreter. Application Firewall prompts recurred because ALF keys decisions to
binary signing identity, while Homebrew and bundled runtime upgrades create new
paths and identities. A pending system prompt can also block synthetic input,
making silent recurrence an operational availability problem rather than a minor
desktop annoyance.

## Decision

ALF membership becomes declarative host convergence. The manifest covers Ava's
current inbound binaries with version-tolerant globs and human-readable purposes;
converge directly adds, unblocks, and prunes rules. Direct mutation was verified
without elevation on macOS 26.5. Compatibility remains fail-open for startup:
retry with non-interactive `sudo -n`, then report an exact manual command.

A host-global watcher observes TCC and ALF logs, coalesces prompt storms, and
closes each incident on a result or a 30-minute escalation. It publishes through
the existing `agent_notices` contract so IM remains the only user-facing delivery
and channel-specific APIs do not leak into system infrastructure.

Granting Full Disk Access to the uv interpreter is retained only as an optional
immediate containment procedure. It is not the recommended direction. The main
line is a unified Ava signing identity for the daemon and helper, with a target of
one Terminal command and one Ava authorization.

## Verification and implementation boundary

The signing direction is design-only in this change. A V1 experiment must first
show that a self-signed CLI/daemon chain is attributed by TCC to a `com.ava.*`
requesting identifier. Signing also has to begin in the user's interactive
Terminal because background and SSH sessions can fail with
`errSecInternalComponent`.

If V1 succeeds and the user confirms the security trade-off, a later change may
provision a dedicated file-based keychain reusable by the update pipeline. That
design limits the key to owner-readable (`0400`) signing use, but deliberately
leaves key exposure, rotation, and recovery for review before implementation.

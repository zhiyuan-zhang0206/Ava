# Fleet release and cutover policies

## Context

The release transition supports one machine; production is a multi-host
cluster whose current code can run the retired in-place updater by itself. The
fleet transition and the one-time production cutover needed five policy
choices before implementation (the plan's open questions).

## Decision

User ruling, 2026-09-27, accepting each recommendation:

1. **Cutover timing.** Cut over as soon as the cutover slices land: production
   first runs the new code from a source checkout (the cutover commit already
   carries the image-exec handoff), then each host adopts retained images one
   at a time. PR #3479 merges inside the planned cutover window, after legacy
   self-updaters are disarmed.
2. **Terminals and schedules are writers.** At a release boundary, persistent
   terminals (including agents' coding sessions) and schedules get a bounded
   wait, then a system-reason cancel/close. They do not survive a release.
3. **Failures before the database fence abort automatically.** Every unit
   restarts its unchanged previous image on the same credentials; no operator
   hold.
4. **Images carry a stable ABI tag** (OS, architecture, libc or macOS major,
   Python ABI) as their compatibility contract. The full platform string is
   provenance only.
5. **Former-gateway material on runner homes** is removed after confirming the
   gateway holds its own copies (notably the backup encryption key), archiving
   the runner copies encrypted and offline first.

## Alternatives rejected

- **Wait for fleet v1 and land directly on images.** Keeps the defective
  legacy updater in production for longer, for a cleaner single jump.
- **Keep terminals and schedules alive across a release.** Old-image processes
  could keep writing after the database fence, voiding the stale-writer
  guarantee.
- **Hold for an operator on a pre-fence failure.** Nothing irreversible has
  happened yet; holding only extends the outage until someone notices.
- **Bind images to the exact platform string.** An OS or kernel patch plus a
  reboot would make a production image refuse to boot.
- **Leave gateway secrets on runners.** A compromised runner would expose
  gateway-level credentials, and cutover scripts would have to reason about
  duplicate material.

## Consequences

- Every release interrupts in-flight agent terminal sessions; agents recover
  their work afterwards, but terminal state is not preserved.
- Releases need image rebuilds only when the ABI tag changes, not on OS patches.
- The cutover runbook must verify the gateway's copy of the backup key before
  any runner copy is archived and removed.

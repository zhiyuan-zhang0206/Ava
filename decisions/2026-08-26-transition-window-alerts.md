# Alert outage episodes as time-graded transitions

## Context

The 2026-08-04 user ruling classifies a node dropping offline, a node being
offline during an update, watchdog self-heal, and recovery after a network
outage as normal operating transitions. They must not immediately raise a
WARNING or ERROR, but an outage that does not recover must not stay hidden.

The machine liveness pass previously coupled alerting to its two-failure
display gate, while the cluster health probe treated a deploy lease as a
binary suppression window. Neither model retained enough episode identity to
grade a continuing outage or escalate it without creating another alert.

## Decision

Every outage episode is a transition measured from its true start. The shared
`transition_severity` policy returns no alert before the warning threshold,
WARNING until the error threshold, and ERROR afterward. Defaults are 180 and
600 seconds, configured cluster-wide through
`AVA_ALERTS_TRANSITION_WARNING_SECONDS` and
`AVA_ALERTS_TRANSITION_ERROR_SECONDS`.

A live cluster deploy lease or a machine's live updater lease explains an
outage for the lease's bounded lifetime. Explanation pauses grading; it does
not reset the episode. If the lease ends while the outage remains open, the
next observation grades from the original start. Unreadable deploy state
explains nothing.

Episode state lives with each producer's durable authority:

- `machine_probe.transition_since` is set by the gateway on the first failed
  machine probe and cleared by the first successful probe. Postgres preserves
  the episode across gateway restarts.
- `$AVA_HOME/health_probe_alert` stores message, start time, and last-fired
  severity. The health probe can therefore track an episode while the gateway
  or database is itself unavailable. Recovery deletes every marker, but only a
  marker with a fired severity resolves an alert instance.

One alert instance represents one episode. Its fingerprint is computed from
stable identity labels and excludes severity; `starts_at` is the episode's
true start. WARNING to ERROR updates the existing row. An increase on an
unresolved, already-notified row is a new firing transition, so the shared
upsert notification gate sends a second IM. Equal severity and downgrades do
not re-notify.

This replaces the rollout-window alert suppression introduced by #1361. The
deploy lease remains the authority, but now acts as an explicit expected
transition window shared by both alert producers rather than as a separate
binary exception.

## Alternatives rejected

**Alert immediately and rely on consecutive probe failures.** Probe cadence
and display anti-jitter are not an incident policy. They cannot express the
three-minute and ten-minute boundaries consistently across producers.

**Suppress every alert while a deploy is active, then start a new timer.**
Resetting at lease release hides the age of an outage that outlives the
rollout. An eight-minute outage is still eight minutes old when its explanation
ends.

**Include severity in the fingerprint.** Escalation would create a second
unresolved instance and recovery would have to guess which duplicate to close.

**Keep episode starts in process memory.** Gateway restarts and OS-scheduled
health-probe invocations would lose the timer and repeatedly grant a fresh
normal-recovery window.

## Consequences

- Normal 30-second to two-minute recovery is silent by default.
- An unexplained open episode fires WARNING at three minutes and escalates to
  ERROR at ten minutes without changing instance identity.
- Deploy windows remain bounded by their existing leases; no second rollout
  timeout is introduced.
- Machine recovery finds unresolved rows by identity labels, allowing rows
  written under the former severity-in-fingerprint convention to resolve.
- The health marker keeps compatibility with one-line direct-IM state and
  two-line pre-transition alert state during upgrade.

# Transition-window alerts

Machine-offline and cluster-health failures now share a three-stage transition
policy: an initial silent window, WARNING after three minutes, and ERROR after
ten minutes. A live deploy explains expected transition failures without
resetting their episode clock, while disk pressure remains unexplained by a
deploy. Severity changes retain one stable alert identity and notify again only
when the class increases.

Machine probe episode starts are durable in Postgres; health-probe episodes use
their existing cluster-home state file. Recovery looks up unresolved rows by
identity labels and replays each persisted fingerprint, so alerts created before
the stable-fingerprint convention are closed rather than stranded. Machine
pause uses the same compatibility rule for expected absence.

The rationale and rejected immediate-alert alternatives are recorded in
[`decisions/2026-08-26-transition-window-alerts.md`](../../../decisions/2026-08-26-transition-window-alerts.md).

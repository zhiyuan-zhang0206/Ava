# The Warnings / Errors card counts alert classes, not events

## Context

The sidebar card showed `warnings_net / errors_net` — raw warning and error event counts less
the dismissed classes. One chatty class made it read 19,629 / 41: the number moved with log
volume rather than with how many distinct things were wrong, and nothing on the card said which
classes they were. Dismissal was already per class (`event_dismissals`), but only reachable
through the API.

## Decision

The card shows the number of **active alert classes** in the selected window, grouped the way an
error tracker groups events: one class per `(level, event_name, source, process)`. The card opens
to a list of those classes by count (count, first/last seen, a lazily-read sample of recent
events) with Dismiss / Reopen per class, reusing `event_dismissals` and the daemon's dismissal
match (`resolution.matching_dismissal`), so the card and the Grafana unresolved gauges agree on
what a dismissed class is. The raw event total stays as secondary text. The dashboard's
`*_dismissed` / `*_net` fields are gone; `alert_classes_active` / `alert_classes_dismissed`
replace them.

## Alternatives rejected

- **Keep event counts, add the class list beside them.** Two numbers for one question; the
  event count is the one that misleads.
- **Group by `(level, event_name, source)` only, dropping `process`.** Closer to an error
  tracker, but `process` is part of the dismissal identity: a process-scoped row would cancel
  only part of a row, which the card could not state honestly.
- **Carry samples in the list response.** Needs a heap read of `attributes` for every class over
  a window up to seven days; the grouped list stays an index-only read and samples are read
  per opened class.
- **A modal for the list.** The card lives inside the stats popover; a dialog opened from it
  unmounts with the popover on the first outside click.

## Consequences

The gauges the daemon publishes keep counting events (unresolved warnings/errors over six
hours); only the dashboard card moved to classes. A class whose dismissal is process-scoped shows
one row per process, so a class can read as partly dismissed.

Supersedes the dashboard half of [2026-08-29-stats-dashboard-resolution-split](2026-08-29-stats-dashboard-resolution-split.md).

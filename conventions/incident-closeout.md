# Incident closeout — the guard-or-repro requirement

Closing gate for incidents and defect fixes: a closeout must answer **"why won't
this recur?"** with either a **guard** (a machine check that fails when the
property breaks) or a **repro** (a minimal runnable case that failed before the
fix). Neither one = the closeout is not accepted.

Origin: task #3811 (from #3578 N1). Field spec + flow: 405. Guard/repro criteria:
3242 (this document).

## The one line

Every behavior-changing closeout carries exactly one `closing-gate:` line
(single line, greppable):

- `closing-gate: guard=<path-or-gate-name> red-battery=<evidence>`
- `closing-gate: repro=<command-or-entry> red/green=<reference> why-not-guard=<one line>`
- `closing-gate: doc_only=<reason>`
- `closing-gate: upline_hook=#<task-id> repro=<...>` — unshipped code: repro now, guard before it ships

**Incidents accept only `guard`.** An *incident* here = a closeout that ships a
user-facing closeout report; when in doubt, treat it as an incident. An incident
already proved human rules do not hold; a repro is an intermediate artifact, not
a terminal state there. A missing or wrong-form line on an incident = BLOCK (no
QA PASS; the incident is not closed and no closeout report ships).

Non-incident closeouts fail softer: a behavior-changing PR without a valid line
and without an applicable exception does not get a QA PASS.

## What counts as a guard

A guard is an asset that **fails when the guarded property is broken**. Forms,
strongest first:

1. Tests (unit / integration / e2e).
2. Static rules (lint / structure gates) — for enumerable shape constraints.
3. Contract gates (CI jobs, schema checks, boot-time self-checks).
4. Monitors / alerts — only for runtime properties CI cannot reproduce.

Every guard must be:

- **Non-vacuous (red battery).** You proved it fails under the target break:
  mutate → red → restore is the minimum evidence. For monitors, the trigger
  condition plus the last real firing. A guard that cannot be shown to fail is
  decoration.
- **Bound to the defect class, not the incident point.** Cover the class — the
  sibling entries / other call sites of the same hole (repo-wide grep), not just
  the exact input that happened to fire.
- **Property, not shape.** Assert the behavior; a shape lock (source text,
  structure regex) is allowed only when the property is not directly testable,
  and it must say why.
- **Attributable** — path + gate name go into the `closing-gate:` line.
- **Cost-matched** — seconds; no new infrastructure.

Not guards: happy-path-only tests; patching the guarded object and asserting on
the patch; "does not raise" assertions; a run you did locally once; stale
snapshots; mock self-loops.

## What counts as a repro

A minimal **runnable** case that failed before the fix:

- **Runnable**: one command / entry point, reproducible in a clean environment —
  the reviewer runs it themselves.
- **Falsified**: the closeout carries the before/after pair (pre-fix failing
  output + post-fix passing output, or the red/green CI run links).
- **Minimal + root-cause-pointed**: strip unrelated config; name the step that
  triggers the root cause. That step is what becomes a guard.
- **Should become a guard**: a repro that can run in CI should be folded into
  one; repros are transition artifacts, not chat attachments.

## Exceptions (explicit, with reasons)

- **Doc-only**: no executable behavior (no scripts, templates, generators, or
  runnable steps) → `doc_only=<reason>`. Does not cover *declared behavior*: a
  runbook step or convention rule a reader will follow is behavior (#2669) —
  give it a check matching what the reader executes.
- **Config values / constants only**: the existing config validation or contract
  snapshot counts as the guard.
- **Unshipped / experimental code** (user ruling 2026-09-15: build it now, no
  scheduling theater): close with `upline_hook=#<task>` + repro; the guard is
  owed **before it ships**, tracked by that task.
- **Field-dependent defects CI cannot reproduce**: a monitor/alert is the guard;
  without one, repro + a follow-up task to add the monitor.
- **External-platform defects**: repro + trigger conditions recorded; filed to
  the upstream/platform task. Do not fake a guard.

## Deliberate cuts

- Not required for every PR — behavior-changing ones only; text/format-only
  changes go through the doc-only exception.
- No coverage metrics (they breed number-chasing).
- No human-signature ceremony (incidents already proved human rules fail;
  incidents must go machine).
- No task-kernel enforcement (deliberate): the gate is the QA verdict + the 405
  closeout check; the task registry does not hard-block a closeout that omits
  the line.
- Repro artifacts do not enter the repo; they become guards or stay in task
  records.

## Where it lands

1. **PR surface**: behavior-changing PR descriptions carry a "Recurrence
   evidence" section reachable from the `closing-gate:` line; QA adds the
   `closing-gate:` verdict line to the review comment (QA validates the red
   battery / red-green pair). An incident PR without a valid guard line does not
   get a QA PASS. The receipt JSON is untouched — trust stays in
   `conventions/qa-approval-receipt.md`.
2. **Task surface**: the closeout note's owner writes the same line; incidents —
   405 verifies it before closing (guard only); non-incidents — spot-checked by
   QA/405, not verified per item.
3. **Report surface**: incident closeout reports include a "Recurrence evidence"
   section quoting the line.

## Case anchors

- #2756 (#3629 root fix): guard = claim-window regression test (red on the old
  implementation) + adversarial battery. Incident → machine.
- #2753: guard = three test files + red/green both sides.
- #2749: guard = the module-set regression (`_CORE_DEFINITION_MODULES`).
- Doc-only example: CHANGELOG-style text → `doc_only`.

# Existing agents enter the new runtime as a closed predecessor

## Context

The retired runtime wrote `agents_meta.incarnation_resources` with process
receipts that carry only a PID and a wall-clock birth. The current model
requires native boot scope (`starttime`, `boot_id`), so those rows no longer
decode. The runtime-birth design forbids a permanent row-adoption path in the
runtime, forbids converting unknown NULL resources into `{}` or a birth
marker, and requires an existing agent to show explicit predecessor and
allocation closure before a successor is admitted. The production cutover
(plan slice FC-4a, consumed by FC-4) needed one representation for such rows
that the ordinary admission accepts once, and nothing that it accepts forever.

## Decision

- **Representation.** The cutover rewrites each retired-shape row to
  `IncarnationResources(generation=G, owner=O, host_process=null,
  frozen_by=null, requests={})`, where (G, O) is the incarnation named by the
  retired value itself. It is exactly what a drained managed row looks like,
  minus the host identity the retired writer could not record.
- **Lifecycle closure** is the existing predecessor receipt rule
  (`resource_admission.PREDECESSOR_RECEIPT`), unchanged: the old drain's
  applied restart still held as the lifecycle pointer, or an applied and
  observed terminate. Admission therefore accepts the form once: it rewrites
  the set for its own incarnation and, for a restart, observes the receipt.
  A same-owner continuation refuses it (no host identity).
- **Allocation closure** is not provable from the database. The operator
  supplies the digest of that machine's closure attestation (recorded
  processes absent or their boot ended, home census empty). The conversion
  stores it, with the before image, operator and reason, on the receipt
  (`payload.cutover_closure`) in the same transaction.
- **Conversion guards** (`shared.predecessor_closure.close_retired_predecessor`):
  exact before-image compare-and-swap; the value must fail the current model
  (NULL and current-model values refuse); the receipt must satisfy the rule for
  the incarnation the before image names; the row may hold no live, different
  or unsettled runtime; the attestation must be for the row's machine; a
  receipt already carrying a closure refuses. Only the one-time cutover script
  calls it, and it is deleted with that script.
- **Unconverted rows stay inadmissible and say why.** A value the current
  model cannot decode raises `ResourceShapeError`; hosted admission records
  `resource_fence` and logs a WARNING naming the cause, and resurrection
  refuses with `runtime_cutover_required`.
- **Drain certification** accepts NULL or the complete, empty, unfrozen set of
  exactly the incarnation the restart released, so converted agents can be
  drained by the next release.
- **Never-admitted resurrection** is a fresh hosted birth only when
  non-admission is proven: every runtime identity field empty and the
  fresh-INSERT birth marker unconsumed, rechecked in the final CAS. A NULL row
  is unknown and still refuses.

## Alternatives rejected

- **Convert retired rows to NULL.** NULL is protocol zero, scheduled for
  deletion, and is not a closure proof; it would also silently demote managed
  rows to the unmanaged exec path.
- **Stamp a birth marker.** A marker asserts the row was never admitted, which
  is false for every existing agent and would erase its predecessor.
- **Teach admission a retired-shape parser** (or a cutover flag). That is the
  permanent compatibility path the design excludes; the runtime would carry
  the retired model forever.
- **A new receipt kind or a dedicated `closed` state.** It would add a second
  acceptance rule next to `PREDECESSOR_RECEIPT`; the existing applied restart
  or observed terminate already states the lifecycle fact.
- **Convert NULL rows in the same step.** They stay protocol zero and remain
  admissible today; converting them switches their exec path to managed
  resources, which belongs to the protocol-zero retirement.

## Consequences

- FC-4 needs, per row: the before image, the receipt id, and the machine
  attestation digest. Rows without a receipt or on machines without
  attestation stay retired-shape: refused, listed, and resolvable later with
  the same function while the cutover script exists.
- Retiring protocol zero must decide the NULL population separately; the same
  representation applies, with its own evidence policy.
- Force-terminating an unconverted retired row still fails at the resource
  freeze (now with the explicit reconciliation message).

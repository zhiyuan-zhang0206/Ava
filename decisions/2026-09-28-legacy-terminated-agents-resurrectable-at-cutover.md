# Legacy terminated agents become resurrectable at the cutover

## Context

Resurrection requires a terminated row to retain its hosted runtime identity:
`runtime_kind='hosted'`, a generation and an owner, and no per-agent pid
(`ops.resurrection_retry.hosted_resurrection_target`, rechecked by the final
CAS in `ops.agent_wake`). Only a row whose fresh-INSERT birth marker proves it
was never admitted resurrects without one. Every other row refuses with
`runtime_cutover_required`.

Agents terminated before the runtime incarnation existed
([2026-09-03](2026-09-03-agent-runtime-incarnation.md)) never received that
identity. Their `incarnation_resources` is NULL and their identity is
incomplete: no kind, a retired `process` kind, a missing generation or owner,
or a leftover pid. The new runtime refuses every one of them for good. The
fleet cutover's database repair (W7, `scripts/cutover_db_records.py`) did not
see them: it converts only retired-shape resource values, and it counted NULL
rows as protocol zero, which hid this population inside that count.

A read-only survey of production on 2026-09-28 found 5,433 such agents on
machines that still exist (macmini 5,327, macbook-air 81, company-mini 25),
912 of them active within the last 30 days, and 160 more on machines that no
longer exist (win, wsl, corpmac).

## Decision

User ruling 2026-09-28: rescue them on cutover day, in W7, by writing the
missing runtime identity directly into the database.

- **What is written.** W7 gains an `identities` step. For each identity-less
  terminated row (status `terminated`, resources NULL, identity incomplete)
  on a machine whose closure attestation W7 holds, it writes
  `runtime_kind='hosted'`, a freshly minted UUID generation and owner, and
  `pid=NULL`. Resources stay NULL. No receipt, birth marker, inbound row or
  other evidence is written.
- **Guards.** One compare-and-swap per machine restates every identity-less
  condition, requires that no lifecycle pointer defers resurrection, and
  compares the row's recorded before image. A row changed since planning is
  left unchanged and listed in the result. The minted values are chosen once,
  at planning, and recorded before the first write, so a continued run writes
  the same values and a later run never mints again (a minted row is no
  longer identity-less).
- **Classification.** The survey lists these rows in D-8 per machine and
  category: `convertible` (the machine's attestation is supplied), `awaiting`
  (a unit remains, no attestation yet), `no_unit` (no unit of the machine is
  left to attest), `paused`, and `pointer` (a lifecycle pointer resurrection
  does not supersede). Only `convertible` rows are written. The rest keep
  refusing and are reported as fenced.
- **Evidence.** The machine closure attestation (every recorded process
  absent, the home census empty) is the allocation-closure evidence FC-4a
  already accepts
  ([2026-09-27](2026-09-27-existing-agent-closed-predecessor-admission.md),
  "Allocation closure"). A probe on the cutover branch showed that a row
  written this way resurrects, that the resurrection CAS clears the identity,
  and that admission then takes the row as protocol zero, on the same path as
  an idling NULL row today. `tests/lifecycle/cutover/test_db_records_identityless.py`
  locks that behavior.
- **Record.** The W7 journal keeps each row's before image, its minted pair
  and the attestation digest. The attestation bytes are kept verbatim in the
  cutover record. Rollback R2 (restoring the cold data directory) is
  unchanged, because the rows written at W7 are restored with everything else.

## Relation to the earlier decisions

This is an explicit exception, not a change of either rule.

- **2026-09-03 (runtime incarnation).** It says the identity is "assigned in
  the same transaction that admits the runtime". The minted identity was never
  admitted. It exists only so the row passes the resurrection gate. The
  resurrection CAS clears it (generation, owner and kind return to NULL), and
  the successor's admission then assigns the real identity. Nothing treats it
  as evidence of an admission, a lifecycle settlement or resource closure.
- **2026-09-27 (FC-4a).** FC-4a rejected a birth marker, because a marker
  claims the row was never admitted, which is false for existing agents. It
  also kept NULL resources as protocol zero. This step writes neither a marker
  nor a resource set, and it rests on the evidence FC-4a accepts. What it adds
  is a runtime identity that no admission produced, confined to the gate that
  requires one.

## Alternatives rejected

- **Do not rescue.** Every one of these agents, including those active in the
  last month, would stay unreachable after the cutover.
- **Forge a receipt (e1).** A synthetic applied-and-observed terminate plus
  closed-predecessor resources. It writes invented lifecycle history that the
  ordinary predecessor rule would then trust as fact. It also turns NULL
  resources into a managed set, a step FC-4a left to the protocol-zero
  retirement.
- **A runtime flag (e3).** Resurrection would accept identity-less terminated
  rows. That is a permanent compatibility path in the runtime, which the
  design excludes.
- **A birth marker.** It claims the row was never admitted, which is false for
  every existing agent (FC-4a).

## Consequences

- After W7 these agents resurrect like any terminated hosted row: by manual
  resurrection, by arriving chat, or through the delivery watchdog. The
  watchdog wakes only a terminated owner that holds a pending chat created
  after its termination and younger than
  `delivery_watchdog_stale_claimed_threshold_seconds` (24 hours by default),
  and it wakes at most `delivery_watchdog_max_resurrect_per_tick` owners per
  tick, each with a cooldown. The population is therefore not woken all at
  once, and older pending chats remain dead letters.
- Rows of a machine without an attestation at W7 still refuse. A later run
  with that machine's attestation, taken while its home is still stopped,
  mints them. Rows of a machine with no unit left, of a paused machine, or
  with a blocking lifecycle pointer keep refusing and are listed as fenced.
- A minted row cannot be told apart from an ordinary terminated hosted row
  with NULL resources. The W7 journal is the only record of which rows were
  minted.

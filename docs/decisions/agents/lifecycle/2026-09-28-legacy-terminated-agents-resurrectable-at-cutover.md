# Legacy terminated agents become resurrectable at the cutover

## Context

Resurrection requires a terminated row to retain its hosted runtime identity:
`runtime_kind='hosted'`, a generation and an owner, and no per-agent pid
(`ops.resurrection_retry.hosted_resurrection_target`, rechecked by the final
CAS in `ops.agent_wake`). Two kinds of row resurrect without one: a row whose
fresh-INSERT birth marker proves it was never admitted, and a row the current
runtime force-terminated while its own lifecycle had left it unowned
([2026-09-29](2026-09-29-unowned-termination-resurrects.md)). Legacy rows
qualify for neither. Every other row refuses with `runtime_cutover_required`.

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
  on a machine whose closure attestation the run holds, it writes
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
  category: `convertible` (the machine's attestation is supplied, was taken
  no earlier than the row's termination, and names the machine's only unit
  left), `awaiting` (a unit remains, no attestation yet), `multi_unit` (more
  than one unit of the machine remains, so its one attestation censuses only
  one of their homes), `no_unit` (no unit of the machine is left to attest),
  `paused`, `pointer` (a lifecycle pointer resurrection does not supersede)
  and `after_attestation` (terminated after its machine's attestation was
  taken). Only `convertible` rows are written. `awaiting` and `multi_unit`
  wait for an input (an attestation, a retired stale unit); the rest keep
  refusing and are reported as fenced.
- **The cutoff.** An attestation proves its home empty when it was taken, so
  it covers only rows terminated no later than its `attested_at`
  (`status_changed_at` comes from the database's clock, `attested_at` from
  the host's). At W7 nothing has terminated an agent since the attestations,
  so the journal's first run refuses while any row reads
  `after_attestation`: it means clock skew (a host clock behind the
  database's would fence legacy rows for good) or a process of the home
  writing after its attestation.
- **Late conversion (W12).** A `pointer` row whose pointer names a forced
  terminate that was applied but never observed converts later when the row
  kept its hosted kind, generation and owner (identity-less through a
  leftover pid) and the force targets exactly that pair: the new agent host
  settles such a force at its first boot on the row's machine (each unit's
  held first start) without changing the row's status, so at W12 a run with
  the W7 attestations mints it. Rows the new code terminated since
  W11 read `after_attestation` and are never minted. A run after the first
  completed one plans no posture effect, so it never touches a unit still
  held.
- **Evidence.** The mint rests on two facts only: the attested home's census
  was empty (no process related to the home, no bound port) when the
  attestation was taken, and the row was terminated no later than that. This
  is a strict subset of the allocation-closure evidence FC-4a accepts
  ([2026-09-27](2026-09-27-existing-agent-closed-predecessor-admission.md),
  "Allocation closure"): FC-4a pairs the attestation with a proof that each
  recorded `(pid, birth)` of the row is gone and with a settled lifecycle
  receipt. An identity-less row has neither. Its resources are NULL, so it
  records no pid and no birth, and the attestation lists none for it (the
  exported rows cover retired-shape values only). What stands in for them is
  that an agent gets a runtime only through admission, admission moves the
  row out of `terminated` (which restamps `status_changed_at`), and so a row
  terminated before an empty census has no live writer on that home. Because
  the census covers one home, a machine with a second unit left gets no mint.
  A probe on the cutover branch showed that a row
  written this way resurrects, that the resurrection CAS clears the identity,
  and that admission then takes the row as protocol zero, on the same path as
  an idling NULL row today. `tests/lifecycle/cutover/test_db_records_identityless.py`
  locks that behavior.
- **Record.** The journal keeps, per run (W7, and W12 appended), each row's
  before image, its minted pair and the attestation digest. The attestation
  bytes are kept verbatim in the cutover record. Rollback R2 (restoring the
  cold data directory) restores the rows written at W7 with everything else,
  and moves the journal aside: its runs describe the replaced database.

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
  nor a resource set, and it rests on a strict subset of the evidence FC-4a
  accepts (see Evidence). What it adds
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
  mints them; rows of a machine with a second unit left convert once the
  stale unit is retired. A `pointer` row converts at W12 when the new agent
  host settled its force, and keeps refusing otherwise. Rows of a machine
  with no unit left or of a paused machine, and rows terminated after their
  machine's attestation (the new code writes this shape too, for an agent
  terminated before its first admission), keep refusing and are listed as
  fenced.
- A minted row cannot be told apart from an ordinary terminated hosted row
  with NULL resources. The database-records journal (its W7 run and any
  later one) is the only record of which rows were minted.

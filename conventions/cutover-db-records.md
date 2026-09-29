# Cutover: repair the gateway database records

One-time procedure of the production fleet cutover. The retired runtime left
records the new code refuses or misreads, and no runtime path repairs them.
Two scripts, deleted after the cutover with the other `scripts/cutover_*`
scripts, handle them:

- `scripts/cutover_db_survey.py` is the read-only half: one snapshot of every
  record the repair touches, the plan's D-series checks (Appendix A plus
  decoding with the current models), the classification of every
  `agents_meta` row whose `incarnation_resources` the current model cannot
  decode, and the per-machine count of identity-less terminated rows.
- `scripts/cutover_db_records.py` is the entry point: `--check` prints the
  survey as JSON, the default mode dry-runs the repairs, `--execute` applies
  them. It is the only caller of anything that writes.

Both run on the gateway, against the home's own PostgreSQL, as the OS-user
administrator acting as the schema owner over the owner-only socket
(`shared.pg_admin.owner_session`). The read modes also work against the legacy
postmaster, which has no custody record; `--execute` additionally binds the
session to the home's postmaster, so it runs only after the data-plane
authority cutover brought the plane up under new custody. Run them with the
`.venv` of a checkout of the cutover commit and `--home` naming the gateway home.

## Order within the runbook

1. **Before the window (T-2).** `--check --legacy-commit <commit every host
   runs>` and store the output. Resolve every `attention` verdict (a pin or
   applied-migration set that would fire a legacy controller, a bootstrap-owned
   database, prepared transactions, a legacy updater, a committed publication):
   the dry run and `--execute` refuse while any remains, naming each check and
   why. Note D-1 (pending publication) and D-2 (deploy lease): their exact JSON
   is the input of the repair.
2. **Baseline (W0).** `--check` again; store it with the cutover record.
3. **Row export (W3, after the gateway's old `ava pause`, before its `ava
   stop`).** Every runner stopped at W2 and the gateway just drained, so the
   rows are final: `--check --rows-out rows.json`. Copy `rows.json` to every
   host.
4. **Closure attestations (after each host's old stop).**
   `scripts/cutover_inventory.py --home ~/.ava --attest rows.json >
   attest-<machine>.json` on every host, the gateway included, before the new
   code starts there: one document per machine, covering that machine's rows
   (each recorded process absent or its boot ended) and the home census (no
   related process at all, no bound port). Exit status 0 means it proves
   closure. It refuses to attest while the home's legacy health probe is
   registered: that probe can start the old code after the document is taken.
   Copy the documents to the gateway unchanged; their bytes are the evidence.
5. **Repair (W7, after `scripts/cutover_db_authority.py`).** Dry-run with every
   input, resolve every refusal, then add `--execute`. In the dry run D-8's
   `counts.identityless.after_attestation` reads 0: the journal's first run
   refuses otherwise (see the clock comparison below). Finally `--check` with
   the same attestations: D-1, D-2 and D-6 read `ok`. D-8 reads `ok` only
   when no retired-shape or identity-less row remains; otherwise it reads
   `fenced` with a count per verdict and reason, and `--check` exits 2 ([rows
   left fenced](#rows-left-fenced)).
6. **Late conversion (W12).** Once every included unit ran the new code, run
   the repair once more for the `pointer` rows the new agent hosts settled
   ([late conversion at W12](#late-conversion-at-w12)).

```bash
P=scripts/cutover_db_records.py
.venv/bin/python $P --home ~/.ava --check --legacy-commit "$OLD" > check-w0.json
.venv/bin/python $P --home ~/.ava --check --rows-out rows.json > check-w3.json
.venv/bin/python scripts/cutover_inventory.py --home ~/.ava --attest rows.json > attest-$(cat ~/.ava/machine_name).json
.venv/bin/python $P --home ~/.ava --attestation attest-gw.json --attestation attest-mini.json ... \
    --pending-json pending.json --lease-json lease.json --retire-units units.json \
    --operator "$OPERATOR" --reason "fleet cutover <id>"
(the same) --execute
```

The row export moves from W0 (the plan) to W3: the old drain at W2 and W3
rewrites rows, and an identity the attestation does not cover keeps its row
inadmissible. A late attestation for a machine is possible only while that
home is still stopped; once the new code runs there, its census is no longer
empty.

## Inputs

| Flag | Content |
|---|---|
| `--attestation FILE` | One `--attest` document per machine; two for one machine refuse |
| `--pending-json FILE` | The exact `pending` object D-1 prints; required when one is recorded, refused when none is |
| `--lease-json FILE` | The exact lease columns D-2 prints; required when a legacy holder, a non-stable phase or a settle hold remains |
| `--retire-units FILE` | `[{"machine", "home", "evidence"}]`: units whose home no longer exists, with why ("host decommissioned", "host confirms the home is absent") |
| `--operator`, `--reason` | Required by `--execute`; recorded in the journal and on every receipt |
| `--legacy-commit REF` | D-3 compares the pin with it; D-4 accepts an applied migration set equal to that commit's or this checkout's |

## What the repair changes

Effects run in this order, each in its own transaction, and compare their row
with the recorded images: the after image means a crashed run already committed
it (`already`), the before image is applied, anything else stops the run. The
conversion re-checks every closure guard under the row lock. The owner session
caps lock waits (`lock_timeout` 10 s) and statements (`statement_timeout`
60 s): an effect that hits a ceiling, for example behind a prepared
transaction holding its row (D-11), rolls back and stops the run with the
cause, and the same inputs continue it.

| Step | Effect |
|---|---|
| `pending` | Removes the durable pending publication; the column returns to SQL NULL. It never runs beside a committed `current` publication: D-1 reads `attention` for one (below), so the run refuses first. |
| `lease` | Releases the legacy deploy lease (phase `stable`, holder, times and settle hold cleared), only once no pending publication remains. |
| `posture` | `paused` postures of included hosts become `idle`, in the journal's first run only: once a run completed, a `paused` posture is a held unit's own (every start inside a hold writes it, W8 and W9 included), and no later run plans a posture effect. No held start precedes that first run: the gateway's `--start` refuses until a run completed, and a runner's joins through the gateway. Paused machines keep theirs; stranded-hold columns stay for the retired-storage cleanup. A `converging` posture or a live updater lease refuses. |
| `units` | Deletes the retired `machine_units` rows. Units of paused machines, this gateway's own unit and attested homes refuse. The stale `machines` row itself stays (the cluster machine-delete endpoint removes it). |
| `incarnations` | `shared.predecessor_closure.close_retired_predecessor` per convertible row: the closed-predecessor form, with the before image, attestation digest, operator and reason recorded on the receipt ([why](../decisions/2026-09-27-existing-agent-closed-predecessor-admission.md)). The journal keeps both before images, the resources and the receipt's payload (NULL included), and both are compared. A row the guards refuse is recorded `refused: <why>` and the run continues. |
| `identities` | One effect per attested machine: each convertible identity-less terminated row takes `runtime_kind='hosted'`, a minted UUID generation and owner, and `pid=NULL`, so resurrection accepts it; the resurrection clears it again. Resources stay NULL; no receipt or other row is written ([why](../decisions/2026-09-28-legacy-terminated-agents-resurrectable-at-cutover.md)). One compare-and-swap restates every identity-less condition and each row's before image. The result is `applied`, `already` (a continued run finds its minted pair), or names the rows that changed since planning, which it leaves unchanged; the run continues. |

D-1 reads `attention` whenever `deployment_state.managed_writer_evidence`
records a committed `current` publication, with or without a pending one. Only
the retired updater's managed-writer mode (`AVA_UPDATE_MANAGED_WRITER`) wrote
one. The new runtime resolves no loaded publication input to match it, so
admission refuses every agent while it stands. The `pending` step would keep
it, but that step is unreachable: the dry run and `--execute` refuse while any
check reads `attention`, before the run's first write. Nothing in this
repository clears a committed publication: contact the maintainers before the
window. SQL NULL evidence reads `ok`, and evidence holding only a pending
publication reads `repair`, which the `pending` step clears to SQL NULL.

`--execute` prints a count per step and outcome, then every result that
carries a note in full (a conversion the guards refused, the rows a mint left
unchanged, `refused: ...` or `applied: minted N, left M ...`), and ends with
`!` instead of `✓` when any result did.

Exit status, every mode: 0 is clean. 1 is a refusal (nothing changed) or an
incomplete run (continue it with the same inputs). 2 means the result needs
the operator's review: `--check` with a check neither `ok` nor `info`, a dry
run with a refusal, or an `--execute` run with a noted result. That run is
recorded complete (the W8 held-start gate and the W11 gate accept it), and the
same inputs only print it again: review each noted row as a
[row left fenced](#rows-left-fenced).

The pending, lease and posture repairs also require an attestation proving
closure from every included machine (a unit neither paused nor retired).

A retired-shape row is `convertible` only with a named incarnation, a row its
successor would take once converted, a settled receipt (the drain's applied
restart held as the lifecycle pointer, or an applied and observed terminate),
and an attestation of its machine that proves every recorded identity gone.
The survey and the conversion judge the row with one rule
(`shared.predecessor_closure.successor_refusal`), which is the successor's own
admission: an idling row with its owner released (admission observes only a
restart pointer), or a terminated row that still records exactly the closed
hosted incarnation and holds no lifecycle pointer (what resurrection requires).
Otherwise the row is `awaiting` (no attestation for its machine yet),
`inadmissible` (it still names a live or different incarnation, the pointer
names an unsettled command, no receipt exists, the machine is paused, no unit
of the machine remains to attest, an identity is unattested or malformed), or
`unconvertible` (a shape no successor accepts even after conversion: a
terminated row whose runtime identity was
released or carries no hosted kind, a terminated row still pointing at its
receipt, a terminate receipt left as an idling row's pointer, a process
runtime). The runtime keeps refusing those rows (`resource_fence`,
`runtime_cutover_required`). NULL rows keep protocol zero.

An identity-less terminated row is one with status `terminated`, NULL
resources and no complete hosted runtime identity (kind not `hosted`, a
missing generation or owner, or a pid): an agent terminated before the runtime
incarnation existed. Resurrection refuses it (`runtime_cutover_required`).
D-8 counts these rows per machine and category (`identityless`, and the
totals under `counts`): `convertible` (the machine's attestation is
supplied and was taken after the row's termination), `awaiting` (a unit
remains, no attestation yet), `multi_unit` (more than one unit of the machine
remains: its one attestation censuses only the home it names, and the mint
has no per-row process proof, so it covers no row of that machine),
`no_unit` (every unit is retired or none is
registered), `paused`, `pointer` (a lifecycle pointer that resurrection would
not settle as superseded: anything but an unapplied restart or terminate), or
`after_attestation` (terminated after its machine's attestation was taken).
Only `convertible` rows are written; the closure attestation, which proves the
home census empty when it was taken, is their evidence, so it covers only rows
terminated before (`status_changed_at` no later than its `attested_at`). That
is less than a retired-shape conversion proves: such a row records no pid, so
the attestation lists no process of it, and no receipt settles it
([why it suffices](../decisions/2026-09-28-legacy-terminated-agents-resurrectable-at-cutover.md)). The
new code writes rows of the same shape, an agent terminated before its first
admission, and those never qualify. The runtime resurrects such a row itself
when the force that ended it recorded an unowned termination
(`decisions/2026-09-29-unowned-termination-resurrects.md`); D-8 still lists it
as `after_attestation`. The comparison crosses clocks (the
database's `now()` stamps the termination, the attesting host's clock stamps
`attested_at`, just after it read the census). Legacy terminations precede
the attestation by at least the drain, and business, the first source of new
terminations, stays closed from W3 until W11, so at W7 no row can read
`after_attestation`. The journal's first run refuses while one does, naming
the machines: that host's clock trails the database's (a row terminated
before the attestation would stay fenced for good), or a process of the home
wrote after its attestation (which then proves nothing). Compare the clocks
or find the writer, then take that machine's attestation again. A host clock
running ahead of the database's could only let rows terminated after the
attestation through: at W7 none exist, and at W12 it would take hours of skew
to reach the first row the new code terminated (after W11).

## Rows left fenced

`--execute` converts only `convertible` rows. Every other retired-shape or
identity-less row stays fenced: the runtime keeps refusing its agent
(`resource_fence` at admission, `runtime_cutover_required` at resurrection;
an identity-less row only at resurrection). D-8 reads `fenced`
with a count and example agent ids per verdict and reason, so `--check` keeps
exiting 2 after the repair. `fenced` is not a refusal: `--execute` prints the
same summary before its first write and records it in its journal run.

Each run re-reads every row. A row whose state changes is reclassified by the
next `--check`, and a later run with new inputs converts it, for as long as
the script exists. What can change a fenced row:

- **`awaiting` or an unattested identity.** Take an attestation for that
  machine with `--attest` while its home is still stopped, before the new
  code starts there. A later run with that attestation converts the rows.
- **A paused machine.** Its rows convert only after the machine is resumed
  and attested while its home is still stopped; a later run with that
  attestation converts them. Known gap: if its host posture still reads
  `paused` (D-6), nothing repairs it, since only the journal's first run
  plans posture effects. No sound late path exists for such a machine, and
  its agents stay fenced.
- **An identity-less row.** `awaiting` converts as above, with a later
  attestation of its machine. `multi_unit` converts once the attested home is
  the machine's only unit left: retire the units whose home no longer exists
  (`--retire-units`, with the attestation, at W7); while two homes of the
  machine are live, none of its rows converts. `paused` needs the machine resumed first, with
  the same posture gap. `no_unit` never converts: no attestation can cover
  it. `pointer` converts only once its pointer is gone, since resurrection
  defers on it whatever identity the row carries: a forced terminate the new
  agent host settles at its first boot converts at the
  [late conversion](#late-conversion-at-w12); any other pointer keeps the row
  fenced. `after_attestation` never converts: once the new code runs on its
  machine, no attestation can prove that home empty. The runtime resurrects
  the ones whose force recorded an unowned termination; the rest stay fenced.
- **Every other reason (known gap).** This covers:
  - a row of a machine with no unit left to attest: every unit is retired
    (`--retire-units`, from the run that retires the last one on) or none is
    registered. An attestation must name a registered unit that is neither
    paused nor retired, so none can ever cover these rows; without this
    verdict they would read `awaiting` and keep D-8 at `repair`;
  - a row that still names a live or different incarnation after every old
    host stopped;
  - a pointer to an unsettled command;
  - no settled receipt (for example a crash-reaped row, which never had a
    terminate command);
  - a malformed value;
  - every `unconvertible` row.

  No input of this script changes these rows. Conversion would not help an
  `unconvertible` row either: neither resurrection nor admission accepts its
  shape. These agents stay fenced until a separate, evidence-backed decision
  exists. The cutover scripts, and this conversion with them, are deleted
  after the cutover.

## Late conversion at W12

An identity-less terminated row whose lifecycle pointer names a forced
terminate that was applied but never observed reads `pointer` at W7 and stays
fenced: resurrection defers on that pointer. The new agent host settles such a
force when it boots on the row's machine, before its scheduler starts
(`shared.hosted_force.recover_orphaned_hosted_forces`, logged as
`hosted boot recovery: observed <n> orphaned force(s)`), but only for a row
that kept a hosted kind, generation and owner (identity-less through a
leftover pid) and a force targeting exactly that pair; every other `pointer`
row stays fenced. It observes the force and clears the pointer without
changing the row's status, so the row keeps its termination time and reads
`convertible` with its machine's attestation. A force whose exec evidence may
still be live is deferred instead (`hosted boot recovery deferred`) and its
row stays fenced.

That boot is each unit's held first start (W8, W9), so by W12 every included
unit has had it. Then, on the gateway:

1. `--check` with the same attestation documents W7 used. Rows the new code
   wrote in the same shape (agents terminated since W11 before their first
   admission) read `after_attestation`: the documents were taken before them,
   so the run never mints them. The runtime resurrects the ones whose force
   recorded an unowned termination; the rest stay fenced.
2. The dry run with those attestations and a new `--reason`; the same inputs
   as W7's completed run change nothing. Pass no `--pending-json`,
   `--lease-json` or `--retire-units`: W7 applied them, and passing them again
   refuses. The plan holds only `identities` and `incarnations` effects. It
   never holds a `posture` effect: a unit still held (a failed W11 smoke)
   reads `paused` in D-6, and the run leaves it so. A `pending`, `lease` or
   `units` effect needs its explicit input; a pending publication or deploy
   lease recorded since W7 refuses the run instead, which means the cluster
   moved since W7: stop.
3. `--execute` with the same arguments, then `--check`: the settled rows are
   gone from `pointer`, and the run is appended to the journal.

```bash
.venv/bin/python $P --home ~/.ava --check --attestation attest-gw.json ... > check-w12.json
.venv/bin/python $P --home ~/.ava --attestation attest-gw.json ... \
    --operator "$OPERATOR" --reason "fleet cutover <id>: W12 late conversion"
(the same) --execute
```

## Record and recovery

`$AVA_HOME/cutover-rollback/db-records/` (0700) holds `journal.json` (0600) and
`attestations/<sha256>.json`, each attestation byte-for-byte. The journal is a
list of runs. A run records its inputs, the fenced summary it printed (which
agents stay fenced, per verdict and reason) and every planned effect with its
before image before the first write, then each effect's result. A run that
plans no effect is recorded too, so its fenced summary is on record. An
`identities` effect records, per row, the before image and the minted
generation and owner; its machine and attestation digest are on the effect. A
crashed run continues only with the same inputs; the same inputs as a
completed run change nothing; new inputs (a late attestation) append a run.
Refusals are all decided before the first write of a run.

Each run also names the adoption it belongs to: the `cutover_id` and
`created_at` of the home's adoption journal. Rollback after the repair (R2)
restores the cold data-directory copy taken at W3, which predates every W7
write, minted identities included; the before images in the journal document
exactly what changed. R2 moves `cutover-rollback/db-records/` aside with the
adoption journal ([rollback](cutover-home-adoption.md#journal-and-recovery)).
A journal whose last run names another adoption than the home's (or the home
has none) is refused by every mode and by the W11 gate: it records repairs of
a database the rollback replaced.

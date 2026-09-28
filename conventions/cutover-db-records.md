# Cutover: repair the gateway database records

One-time procedure of the production fleet cutover. The retired runtime left
records the new code refuses or misreads, and no runtime path repairs them.
Two scripts, deleted after the cutover with the other `scripts/cutover_*`
scripts, handle them:

- `scripts/cutover_db_survey.py` is the read-only half: one snapshot of every
  record the repair touches, the plan's D-series checks (Appendix A plus
  decoding with the current models) and the classification of every
  `agents_meta` row whose `incarnation_resources` the current model cannot
  decode.
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
   database, prepared transactions). Note D-1 (pending publication) and D-2
   (deploy lease): their exact JSON is the input of the repair.
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
   Ava process, no bound port). Exit status 0 means it proves closure. Copy the
   documents to the gateway unchanged; their bytes are the evidence.
5. **Repair (W7, after `scripts/cutover_db_authority.py`).** Dry-run with every
   input, resolve every refusal, then add `--execute`. Finally `--check` with
   the same attestations: D-1, D-2, D-6 and D-8 read `ok`, and every
   remaining retired-shape row is listed `inadmissible` or `unconvertible`
   with its reason.

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
conversion re-checks every closure guard under the row lock.

| Step | Effect |
|---|---|
| `pending` | Removes the durable pending publication. The column returns to SQL NULL when nothing was ever published, else keeps `current`; either result must decode for admission. |
| `lease` | Releases the legacy deploy lease (phase `stable`, holder, times and settle hold cleared), only once no pending publication remains. |
| `posture` | `paused` postures of included hosts become `idle`. Paused machines keep theirs; stranded-hold columns stay for the retired-storage cleanup. A `converging` posture or a live updater lease refuses. |
| `units` | Deletes the retired `machine_units` rows. Units of paused machines, this gateway's own unit and attested homes refuse. The stale `machines` row itself stays (the cluster machine-delete endpoint removes it). |
| `incarnations` | `shared.predecessor_closure.close_retired_predecessor` per convertible row: the closed-predecessor form, with the before image, attestation digest, operator and reason recorded on the receipt ([why](../decisions/2026-09-27-existing-agent-closed-predecessor-admission.md)). A row the guards refuse is recorded `refused: <why>` and the run continues. |

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
names an unsettled command, no receipt exists, the machine is paused, an
identity is unattested or malformed), or `unconvertible` (a shape no successor
accepts even after conversion: a terminated row whose runtime identity was
released or carries no hosted kind, a terminated row still pointing at its
receipt, a terminate receipt left as an idling row's pointer, a process
runtime). The runtime keeps refusing those rows (`resource_fence`,
`runtime_cutover_required`). NULL rows are counted and left as protocol zero.

## Record and recovery

`$AVA_HOME/cutover-rollback/db-records/` (0700) holds `journal.json` (0600) and
`attestations/<sha256>.json`, each attestation byte-for-byte. The journal is a
list of runs. A run records its inputs and every planned effect with its before
image before the first write, then each effect's result. A crashed run
continues only with the same inputs; the same inputs as a completed run change
nothing; new inputs (a late attestation) append a run. Refusals are all
decided before the first write of a run.

Rollback after the repair (R2) restores the cold data-directory copy taken at
W3; the before images in the journal document exactly what changed.

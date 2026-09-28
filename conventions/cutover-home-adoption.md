# Cutover: adopt a legacy-born home

One-time procedure of the production fleet cutover. A home born by legacy code
has no `start-intent.json`, no `AVA_SERVICE_PATH`, legacy OS jobs and legacy
state files; a former gateway now serving as a remote agent-runner also carries
a gateway-shaped registry record and gateway-only material. Ordinary
`ava start` never adopts such a home. Two scripts do it explicitly, and both
are deleted with the other `scripts/cutover_*` scripts after the cutover:

- `scripts/cutover_inventory.py` is read-only. It prints a JSON verdict: the
  home's role, identity, registry record and port block, legacy files, the
  pause-owner journal, gateway-only material (names only, never values), legacy
  OS jobs matched by the exact home slug, live processes related to the home,
  bound ports, a reviewed-PATH candidate, the refusals, and the exact plan
  adoption would execute now.
- `scripts/cutover_adopt_home.py` executes that plan. Dry-run is the default;
  `--execute` adopts; `--start` performs the held first start; `--resume`
  releases the cutover hold at the go/no-go gate. `--execute`
  also requires `--expect-mode gateway|remote-unit`: the mode is inferred from
  the capability files alone, and a remote unit's adoption strips credentials
  and moves `pg/`, `backups/` and `secrets/`, so a mismatch refuses.

Run both with the `.venv` of the checkout that owns the home (`$AVA_HOME/source`
for production, or a checkout whose `.ava_home` names the home). A home with
its own `source` checkout adopts and starts only from it; a throwaway
worktree whose `.ava_home` names it (the T-3 dry-run) may only plan, so the
intent never records a disposable checkout. Never run them against a home you
are not converting. While the cutover hold the adoption
journal records stands, an ordinary start never releases it
(`cli/cutover_hold.py`): before the held first start (`--start`) a bare
`ava start` refuses and names that command, and after it (for example the
autostart job after a reboot) a bare start brings the unit up still held. A
held first start that failed or was not ready leaves phase `starting`; the
next start that passes readiness, `--start` or a bare one, completes it to
`ready`. `ava cluster recover` and `ava maintenance resume` refuse that hold
too and name the one exit, the go/no-go step `--resume`, which alone opens
business.

## Order within the runbook

1. Before the window, on every host: run the inventory, choose
   `AVA_SERVICE_PATH` from its `service_path_candidate` (virtualenv, home and
   injected directories already removed; review it), and run the adoption
   dry-run. Resolve every refusal before the window. `--attest ROWS.json`
   prints this machine's one closure attestation for the database-records
   repair: each of its recorded legacy `(pid, birth)` identities absent or
   predating the current boot, plus the home census (no process related to the
   home, Ava service or not, and no bound port). A live pid proves nothing
   unless its birth, read with the primitive the legacy code wrote it with,
   rules it out: a reading within 5 s is the recorded process, a Linux reading
   further off stays `unknown` (wall-clock steps move it), and only a birth
   300 s before the current boot reads `boot_changed`. Close every shell or
   tool whose working directory is inside the home first; the script's own
   shell is exempt, but both scripts refuse to run inside an Ava process of
   the home (its terminal, an agent), whose census would skip that process.
   Run it after the host's old stop, over the rows exported after the W3
   drain ([database records](cutover-db-records.md)).
2. Disarm and stop with the old code (runbook W1 to W3). The adoption refuses
   while any Ava process of the home is alive or a data-plane port is bound.
3. Gateway (W5): `--execute`, then the data-plane authority cutover
   (`scripts/cutover_db_authority.py`, see
   [convert an existing home](data-plane-secret-split.md#convert-an-existing-home)),
   the [database records repair](cutover-db-records.md), then `--start` (W8).
4. Each runner (W9): check out the new commit, `--execute`, then
   `--start --db-capability <bundle>` with the bundle the gateway's data-plane
   cutover issued for it (step `remote-units`) and its transport key in
   `AVA_DB_CAPABILITY_KEY`: the runner no longer holds the human bearer, and
   its capability both authenticates it and carries its database login.
5. At the go/no-go gate (W10), verify the gate's checklist, then (W11) release
   each hold with `--resume`, the gateway first, then the runners. It takes
   the hold's identity from the journal and refuses unless the adoption
   completed, the hold stands in phase `ready` (the held first start passed
   readiness) and, on a gateway, the database-records repair recorded a
   completed run with no incomplete one after it (W7). The release itself is
   `ava maintenance resume`'s: the unit must be serving, and the agents the
   hold drained are woken. Everything else the gate lists (smoke agents, the
   alert path, an empty legacy census on every host, the stale-writer probe,
   the gateway released before the runners) no single host can check, so it
   stays the operator's.

```bash
.venv/bin/python scripts/cutover_inventory.py --home ~/.ava --service-path "$REVIEWED_PATH"
.venv/bin/python scripts/cutover_adopt_home.py --home ~/.ava --service-path "$REVIEWED_PATH"
.venv/bin/python scripts/cutover_adopt_home.py --home ~/.ava --service-path "$REVIEWED_PATH" \
    --execute --expect-mode remote-unit    # gateway on the gateway
.venv/bin/python scripts/cutover_adopt_home.py --home ~/.ava --start
.venv/bin/python scripts/cutover_adopt_home.py --home ~/.ava --resume    # go/no-go (W11)
```

`--registry` names the cluster registry when the home's `.env` does not declare
`AVA_CLUSTER_REGISTRY` and it is not `~/.ava/clusters.json`.

## What adoption changes

Steps run in order, each recorded `started` (with its planned effects) before it
acts and `done` after:

| Step | Effect |
|---|---|
| `jobs` | Retires this home's legacy OS jobs: crontab lines first (they carry the Linux auto-rollback probe and the watchdogs), then launchd labels in disarm order (health probe, watchdog probes, hold watchdog, autostart, Gate, LGTM, logs, packages, PR flow), then Linux units (`ava-boot.<slug>.service` through `sudo -n`, Gate and LGTM user units). The permissions helper is kept; the new converge rebuilds and reloads it. |
| `hold` | Adopts the maintenance hold a completed legacy `ava stop` left (phase `stopped`) as the cutover hold, so the final resume wakes exactly the agents that stop drained. Without one, it archives an inert resumed pause-owner journal and creates a fresh cutover hold in phase `stopped`. |
| `files` | Moves inert legacy files aside: `installed_sha`, `deploy-state.json`, `cluster_paused`, probe counters, updater locks and flags, session records, the hold-watchdog attempt, stale `run/*.pid`, the legacy boot script. |
| `selection` | Translates a non-empty `disabled_services` into `service-selection.json` (`except` mode) and moves the legacy file aside. |
| `env` | Records `AVA_SERVICE_PATH` exactly as supplied (never from the caller's PATH) and removes dead keys. On a remote unit it removes the gateway-only keys (owner, admin and runner passwords, data-plane URLs, `AVA_PITR_*`) and the human bearer `AVA_CLUSTER_SECRET` (the gateway's cutover rotates it; a remote unit authenticates with its capability's API token and a runner home still holding it refuses to start). Other keys, including model API keys, are untouched. |
| `residue` | Remote unit only: moves former-gateway material aside (`backups/`, `physical-backup/`, `masked-backup-*`, `redis/`, `pgbouncer/`, `pg/`, `secrets/*` except `--keep-secret` names, `run/bootstrap-snapshot.json`). The host-level `pg-template-17` beside the registry and `runtime/` stay. |
| `record` | Gateway: adds any port key of the current block the record lacks (for example the release coordinator's `coordinator` slot), at the block position (the default home uses its fixed ports), refusing a collision. Remote unit: retires the gateway-shaped record after the data plane was proven absent. |
| `intent` | Writes `start-intent.json` in phase `provisioned`: the existing registry record (gateway) or none, the persisted machine identity and capabilities, `AVA_SERVICE_PATH`, `checkout` = the owning checkout, `worktree=false`. Nothing is re-minted. |

The run ends by passing the adopted home through the same identity preparation
`ava start` performs first. Nothing is deleted: everything moved lives under
`$AVA_HOME/cutover-rollback/` (`home-files/`, `residue/`, `os-jobs/` with the
plists, unit files and `crontab.before`), private to the OS user.

## Journal and recovery

`$AVA_HOME/cutover-rollback/adopt-home.json` (0600) records the inputs, the
cutover hold identity (`origin` `legacy-stop` or `cutover`) and each step's state
and effects. The first `--execute` writes it only after every refusal check
passed. A crashed run continues from it with the same inputs; a completed
journal re-verifies and changes nothing. A different `--service-path`,
`--keep-secret`, registry or checkout on a continuation is refused.

Refusals, all before the first effect: a live Ava process of the home (the
kept helper excepted), a pidfile naming one, a bound data-plane port, a destroy
intent, a start intent this adoption did not write, a pause that is not a
completed stop's maintenance hold, crontab lines nobody can attribute, an
unreadable crontab, a missing or unnormalized `AVA_SERVICE_PATH` or one that
differs from the declared value, a home without a persisted machine name
(neither `machine_name` nor `AVA_MACHINE_NAME`: write the unit's
`machine_units` name into `$AVA_HOME/machine_name` first; the fleet plan
expects this on company-air and company-mini, and `--attest` needs the same
name), a gateway without a registry record or data
plane URLs, a remote unit without a gateway URL, `.env` and record port
conflicts, disagreeing capability declarations, a new port that collides, and
an archive path that is already taken.

Rollback before the first held start (R0/R1): boot the new code back out,
move the archived files, residue, plists and unit files back to their original
paths, reinstall `crontab.before`, restore the registry record from the
journal's `record-retire` effect, delete `start-intent.json`, and restore `.env`
from its pre-adoption snapshot in `backups/env/` (moved to
`cutover-rollback/residue/backups/env/` on a remote unit).

### An unreadable adoption journal

The journal is written atomically, so only a hand edit or disk damage makes it
unreadable. Nothing can then tell whether the standing hold is the cutover
hold, so every path that asks fails closed with "unreadable adoption journal":
a bare `ava start` (the autostart job included), `ava cluster recover`,
`--start` and `--resume`. That also blocks the ordinary start of every later
hold on the home, not only the cutover's.

- **A later hold** (a stop after the cutover hold was released): read its
  exact holder and time with `ava maintenance status`, then run
  `ava maintenance start --operation <holder> --acquired-at <time>` and
  `ava maintenance resume --operation <holder> --acquired-at <time>`. Neither
  needs the journal; the resume notes that it could not read it. Every
  holder but `cutover:…` resumes this way: a later stop's (`local-pause:…`)
  and a hold another subsystem took (`fleet:…`, `pitr:…`, `recovery:…`).
- **The cutover hold itself**: that recovery would release it without the
  go/no-go step's checks. A created cutover hold (holder `cutover:<id>`) is
  refused outright; an adopted legacy stop's hold is a `local-pause:` holder
  the resume cannot tell from a later one, so do not use it there. Repair the
  journal and use `--resume`. If it cannot be repaired, verify those checks
  by hand first (the adoption steps all `done` as far as the damaged file
  shows, phase `ready` in `ava maintenance status`, on a gateway a `done`
  last run in `cutover-rollback/db-records/journal.json`), record that in the
  cutover record, and only then move the damaged `adopt-home.json` aside:
  the exact-holder resume then releases the hold as on a never-adopted home.

A *missing* journal instead reads as a home that was never adopted, and an
ordinary start then releases the hold like any other. Never delete or move
`cutover-rollback/` while the cutover hold stands; the one exception is an
unrepairable journal after the checks above.

## A legacy stop hold with failure receipts

Adoption keeps only a completed legacy stop's hold: phase `stopped` with no
unsettled failure receipt. Any other paused journal refuses ("holds a pause
that is not a completed stop's maintenance hold"). Settle it with the old code,
before the host's code switch; read the receipts with the old
`ava maintenance status`.

- The drain failed (phase `preparing`, `draining` or `drained`; the old
  `ava stop` printed "continuations failed; hold retained"). Fix the named
  agents' root cause, run the old
  `ava maintenance repair --operation <holder> --acquired-at <time>` (it
  records the operator and moves the receipts into `repaired`), then re-run
  the old `ava stop --yes`. The repair **releases** that hold ("hold
  released"): the unit's posture returns to `idle`, admission reopens and
  the drained agents are woken. The re-run stop then drains again under a
  fresh holder (`local-pause:<machine>:<pid>:<uuid>`) and takes that new hold
  to `stopped`; the inventory reports the new one adoptable, and it is the
  generation the adoption records. The repair needs the gateway database,
  so a runner settles before the gateway's W3 stop.

  Between the repair and the re-run stop, business is briefly open again on
  that unit while the rest of the fleet stays closed: its agents may run
  turns and rewrite their rows. Consequences for the window: re-run the stop
  at once and budget a second drain for that unit; take the W3 row export
  ([database records](cutover-db-records.md)) only after that unit's last
  drain completed, since the rows must be final; and record the reopening
  and the new holder in the cutover record.
- **Known gap:** the hold reached `stopped` with a receipt latched after its
  drain (a turn failing while services stopped). Neither code base has a
  sanctioned exit: `repair` and `resume --cancel` refuse a started stop, and
  start and resume refuse unsettled receipts. Never edit the journal by hand.
  Until an exit exists, record the hold and its receipts, then exclude that
  runner from the window (it stays on the old code, stopped, and
  `--exclude-unit` at W6 keeps it fenced), or treat it as a no-go on the
  gateway (R1).

## Operator follow-up

- Remote units: the archived residue and `.env` snapshots hold gateway
  credentials and backups. Confirm the gateway holds its own copies (the backup
  encryption key first: after the gateway's `api` cutover step that is the
  pinned `$AVA_HOME/backups/logical-backup.passphrase`, the only key to every
  logical backup of the home, earlier and later — escrow it with the
  gateway's other backup keys), archive the runner copies encrypted and
  offline, then delete `cutover-rollback/residue/`.
- Keep the hosts awake and on AC until the holds are released: after the first
  start the new converge registers the autostart job again. A reboot's ordinary
  start keeps the cutover hold, but the unit is down until it is ready again.
- Delete `cutover-rollback/` after the agreed retention period, and never
  before `--resume` released the cutover hold.

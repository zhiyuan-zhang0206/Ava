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
`ava start` refuses and names that command, as does `ava maintenance start`
with the hold's exact generation, and after it (for example the
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
   dry-run. Resolve every refusal before the window, except the two the window
   itself clears: live home processes (W2, W3) and, on a gateway, its legacy
   health probe (W1). `--attest ROWS.json`
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
   W1 unregisters the gateway's health probe (old
   `ava cluster health-probe-unregister`), whose `--auto-rollback` would roll
   the stopped home back and start the old code. Every start of the old
   gateway registers it again (its lifespan does), a watchdog restart
   included: never restart the old gateway after W1, and run the old
   unregister once more right before the gateway's old stop (W3), after which
   nothing registers it. The closure attestation and the adoption refuse while
   the probe is registered. Found at the adoption, it means the old gateway
   started after its attestation, so that attestation and the W3 row export
   no longer describe the home: roll back (R1) and repeat W1 to W3.

   W2 stops the runners **one at a time**, never in parallel: start a
   runner's old `ava stop --yes` only after the previous runner's returned,
   then read that runner's old `ava maintenance status` and classify any
   failure receipt ([below](#a-legacy-stop-hold-with-failure-receipts))
   before the next one. Every old drain publishes a wake for each agent of
   its cohort on the cluster-wide wake channel, and every other runner's agent
   host starts a turn for each. A runner whose stop terminates its agent host
   while another runner drains cancels those turns before their row read can
   say "not ours", and the old host latches each one as a failure receipt of
   its own hold (FC-10 F20). In sequence, no drain's wakes reach another
   runner's stop. The rest of the fleet's wakes stay: until W3 the gateway and
   its own agents publish them (heartbeat check-ins, the delivery watchdog
   re-waking queued messages, schedules, IM), and the old code cannot silence
   them short of stopping the gateway, which must come last. So no check can
   prove a runner's stop free of wakes. On a `stopped` hold the adoption
   settles every receipt they leave, given the legacy rules; the sequence
   stays as defence in depth. Budget the drains in sequence.

   Once a runner is stopped in the window, never run a start of the old code
   on it: no `ava start`, no `ava cluster update`, no stranded-hold recovery
   (the `[hold-recover]` entry the old pause controller and hold watchdog
   spawn). A start that succeeds releases the hold and reopens business on
   the old code inside the window; one that meets a latched receipt refuses
   and leaves the hold in `starting` (FC-10: the LX hold), which the adoption
   never keeps, since it keeps only a `stopped` hold. The released unit needs
   a second drain, and the W3 row export must follow it; the stuck one can
   only be excluded (W6 `--exclude-unit`) or the window rolled back (R1).
3. Gateway (W5): `--execute`, then the data-plane authority cutover
   (`scripts/cutover_db_authority.py`, see
   [convert an existing home](data-plane-secret-split.md#convert-an-existing-home)),
   the [database records repair](cutover-db-records.md), then `--start` (W8).
   `--start` refuses on a gateway until the repair recorded a completed run,
   with the check `--resume` makes (W11): the start writes the host's `paused`
   posture, which the repair's first run would set `idle`. A runner's
   `--start` has no such check; it joins through the gateway's bootstrap,
   which serves only once the gateway's own held start ran.
4. Each runner (W9): check out the new commit, `--execute`, then
   `--start --db-capability <bundle>` with the bundle the gateway's data-plane
   cutover issued for it (step `remote-units`) and its transport key in
   `AVA_DB_CAPABILITY_KEY`: the runner no longer holds the human bearer, and
   its capability both authenticates it and carries its database login.
5. At the go/no-go gate (W10), verify the
   [held gate](#the-held-gate-and-the-staged-release), then (W11) release each
   hold with `--resume`, the gateway first, each release followed by a smoke
   agent on that unit before the next one. It takes
   the hold's identity from the journal and refuses unless the adoption
   completed, the hold stands in phase `ready` (the held first start passed
   readiness) and, on a gateway, the database-records repair recorded a
   completed run with no incomplete one after it (W7). The release itself is
   `ava maintenance resume`'s: the unit must be serving, and the agents the
   hold drained are woken. Everything else (the gate's cross-host checks, the
   smoke agents, the gateway released before the runners) no single host can
   check, so it stays the operator's.
6. Close-out (W12): every unit's held first start booted its new agent host,
   which settles the forced terminates of hosted rows that left some
   identity-less rows fenced as `pointer` at W7. The gateway runs the
   database-records repair once more with the W7 attestations and a new
   `--reason`; it mints only rows terminated before their machine's
   attestation ([late conversion](cutover-db-records.md#late-conversion-at-w12)).

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

## The held gate and the staged release

No agent runs while a cutover hold stands, so the gate (W10) holds no smoke
agent. The gateway answers every business route with 503 except its
control-plane routes (`/api/cluster/*`, `GET /api/health`, `POST /api/alerts`,
`POST /api/work-failed` and `GET /api/bootstrap`), so a spawn never reaches a
unit. Bootstrap stays open because each runner's held first start (W9) joins
through it and every runner process reads its configuration from it. The hold
changes neither its authentication nor its payload: it still requires a
bearer, which is the human cluster secret or the active write generation's
machine API token of either class (gateway or runner), and it serves neither
a database login nor the human secret. A unit's `/ops` admits only its
readiness probe; the agent host builds no runtime for a held unit and the
schedule manager fires nothing. Opening any of these for one smoke agent
would open the path business uses, so the gate stays what the held units
prove, on every included unit:

- `ava status` ready and naming the generation; `ava maintenance status`
  shows the cutover hold in phase `ready`;
- business still closed: an authenticated `POST /api/agents` answers 503
  (`cluster_updating`), `GET /api/health` answers 200; `GET /api/bootstrap`
  answers 401 without a bearer and 200 with the new human bearer (the
  credential the operator holds, as for the cluster routes below), and its
  `AVA_DB_URL` carries no password and no `AVA_CLUSTER_SECRET` key appears.
  The runner token needs no separate check: each runner's held first start
  (W9) fetched bootstrap with it, and a refused fetch fails that start;
- the new code reads the production records: `GET /api/cluster/status` and
  `GET /api/cluster/roster` answer 200 with the new bearer;
- the alert path: a test `POST /api/alerts` lands a row in `alerts` and reaches
  the out-of-band channel;
- an empty legacy census on every host (the inventory lists no legacy job);
- the stale-writer probe: the pre-cutover runner database login, human bearer
  and both Redis passwords (the W3 copies) are refused.

That agents run on this commit is proven before the window: the cutover
rehearsal (FC-10) at exactly this commit spawns agents through gateway →
`/ops` with the secret set, and the Linux A→B→A proof runs them through a
release. W10 is the last point of exact rollback (R2), and it therefore covers
no smoke of the production units themselves.

The release (W11) is staged. `--resume` the gateway, then run a smoke agent on
the gateway's own machine: spawn one there (`POST /api/agents` with `machine`
naming it) and have it run `print(1 + 2)`; its code output is `3`. Only then
release a runner and smoke it the same way, one runner at a time. Business is
open from the gateway's release on, so a failed smoke is repaired forward
(R3); restoring the W3 dump instead is an explicit user decision that loses
what business wrote since. Stop the release at the failed unit: units not yet
released stay held, their agents asleep and their inbound messages queued,
until the failure is understood.

## What adoption changes

Steps run in order, each recorded `started` (with its planned effects) before it
acts and `done` after:

| Step | Effect |
|---|---|
| `jobs` | Retires this home's legacy OS jobs: crontab lines first (they carry the watchdogs; the auto-rollback health probe must already be gone, see the refusals), then launchd labels in disarm order (health probe, watchdog probes, hold watchdog, autostart, Gate, LGTM, logs, packages, PR flow), then Linux units (`ava-boot.<slug>.service` through `sudo -n`, Gate and LGTM user units). The permissions helper is kept; the new converge rebuilds and reloads it. |
| `hold` | Adopts the maintenance hold a completed legacy `ava stop` left (phase `stopped`) as the cutover hold, so the final resume wakes exactly the agents that stop drained. Its failure receipts, which carry no lost work, are recorded by class and settled ([below](#a-legacy-stop-hold-with-failure-receipts)). Without one, it archives an inert resumed pause-owner journal and creates a fresh cutover hold in phase `stopped`. |
| `files` | Moves inert legacy files aside: `installed_sha`, `deploy-state.json`, `cluster_paused`, probe counters, updater locks and flags, session records, the hold-watchdog attempt, stale `run/*.pid`, the legacy boot script. |
| `selection` | Translates a non-empty `disabled_services` into `service-selection.json` (`except` mode) and moves the legacy file aside. |
| `env` | Records `AVA_SERVICE_PATH` exactly as supplied (never from the caller's PATH) and removes dead keys, the legacy keys no setting reads any more (`DEAD_KEYS` in `scripts/cutover_inventory.py`). On a remote unit it removes the gateway-only keys (owner, admin and runner passwords, data-plane URLs, `AVA_PITR_*`) and the human bearer `AVA_CLUSTER_SECRET` (the gateway's cutover rotates it; a remote unit authenticates with its capability's API token and a runner home still holding it refuses to start). Other keys, including model API keys, are untouched. |
| `residue` | Remote unit only: moves former-gateway material aside (`backups/`, `physical-backup/`, `masked-backup-*`, `redis/`, `pgbouncer/`, `pg/`, `secrets/*` except `--keep-secret` names, `run/bootstrap-snapshot.json`). The host-level `pg-template-17` beside the registry and `runtime/` stay. |
| `record` | Gateway: adds any port key of the current block the record lacks (for example the release coordinator's `coordinator` slot), at the block position (the default home uses its fixed ports), refusing a collision. Remote unit: retires the gateway-shaped record after the data plane was proven absent. |
| `intent` | Writes `start-intent.json` in phase `provisioned`: the existing registry record (gateway) or none, the persisted machine identity and capabilities, `AVA_SERVICE_PATH`, `checkout` = the owning checkout, `worktree=false`. Nothing is re-minted. |

The run ends by passing the adopted home through the same identity preparation
`ava start` performs first. Nothing is deleted: everything moved lives under
`$AVA_HOME/cutover-rollback/` (`home-files/`, `residue/`, `os-jobs/` with the
plists, unit files and `crontab.before`), private to the OS user.

### Units installed by hand

The `jobs` step matches only the names legacy code registered. The production
gateway also carries units installed by hand, which no code registers, the new
code included. The adoption leaves them in place:

| Unit | What it is | In the window |
|---|---|---|
| `com.ava.tempo.<slug>.service` (user) | The Tempo trace store the collector exports to (`AVA_TELEMETRY_TEMPO_ENDPOINT`, `AVA_TELEMETRY_TEMPO_QUERY_URL`), listening on `127.0.0.1` ports 3200, 14318 and 9095; binary, configuration and data under `$AVA_HOME/lgtm/tempo/` | Stop it after the gateway's old stop (W3), before its closure attestation, and start it again once the held first start (W8) passed, or after a rollback's old start |
| `com.ava.tempo-pulse.timer` and `.service` (user) | Every 5 minutes `$AVA_HOME/bin/tempo-pulse.sh` probes Tempo, restarts it after three failed probes and reports the restart with `ava agents send` | Stop the timer with Tempo; start it after Tempo |
| `ava-gateway-egress@.service` (system template, one enabled instance per network interface) | Egress traffic shaping with `tc` from `/etc/ava/gateway-egress.conf`: a oneshot with no process, no home path, no port and no Ava verb | Untouched |

Tempo's process runs from inside the home, and so does a pulse run: while
either is alive the home census is not empty, the attestation does not prove
closure and the adoption refuses (`live home process ... must stop first`).
Nothing else conflicts with the new code: no port of the home's block is
Tempo's, the native Loki's gRPC port (default 9095) keeps the override the
gateway's `.env` declares (`AVA_LGTM_LOKI_GRPC_PORT`), the new code touches
only `lgtm/native/` of the home's `lgtm/`, and it keeps the `ava agents send`
verb the pulse calls. Stop, do not disable: a reboot inside the window starts
both again, so stop them again before an attestation or adoption.

```bash
systemctl --user stop com.ava.tempo-pulse.timer "com.ava.tempo.$SLUG.service"    # W3, after the old stop
systemctl --user start "com.ava.tempo.$SLUG.service" com.ava.tempo-pulse.timer   # after W8, or R1/R2
```

The PR-flow crontab line carries an older unslugged `# ava-pr-flow` comment in
its command, before its marker `# ava-pr-flow.<slug>`. It is one line, owned
through that exact marker: W1 removes it by marker and the adoption retires it
whole. No separate unslugged line exists.

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
completed stop's maintenance hold (a `stopped` one with a receipt it cannot
settle included), a registered legacy health probe (before
the `jobs` step), crontab lines nobody can attribute, an
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
`cutover-rollback/residue/backups/env/` on a remote unit). Last, move
`cutover-rollback/adopt-home.json` aside (keep it, renamed, as the record of
what was undone), whatever step the adoption reached: a completed one, or
one that stopped partway (a step failed or the run crashed). While it exists,
a retry would take the home as adopted or continue the recorded run, and once
it records the `jobs` step (`started` or `done`) the attestation and the
adoption skip their guard against a re-armed legacy health probe. Only a
journal whose `intent` step is `done` while the home has no start intent is
refused (by the inventory, the attestation and the adoption); a partial
adoption's journal is accepted without a word, so moving it aside is the only
thing that re-arms that guard.

Rollback after the data-plane cutover (R2, before any `--resume`): stop the
new code fully on every unit (`ava stop --yes`, data plane included), then on
the gateway replace `pg/` with the cold copy taken at W3 and restore the
pre-adoption `.env` from the W3 copy of the home configuration (`backups/env/`
holds it too, until its 20-snapshot rotation drops it). For Redis and
PgBouncer that `.env` is all R2 restores: every legacy start renders
`redis/redis.conf` (its `requirepass`), `pgbouncer/pgbouncer.ini` and
`pgbouncer/userlist.txt` from it and re-affirms the Redis runtime user with
its password, replacing whatever the data-plane cutover wrote there, as long
as no Redis or pooler of the new code still runs. Move `db-authority/` aside
as well: its journal records the conversion done, so a later
`scripts/cutover_db_authority.py` would take a plane that is legacy again as
converted. For the same reason move `cutover-rollback/db-records/` aside: its
journal records W7 done, so the W11 gate would pass and a retried W7 would
return the old run or run as a late one, which repairs no posture. Each run
names the adoption it belongs to, and the repair and the W11 gate refuse a
journal whose last run names another adoption than the home's. Then R1 on
every host.

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
  The database-records repair then refuses its journal, which names that
  adoption, so no W12 late conversion runs there: its `pointer` rows stay
  fenced.

A *missing* journal instead reads as a home that was never adopted, and an
ordinary start then releases the hold like any other. Never delete or move
`cutover-rollback/` while the cutover hold stands; the exceptions are an
unrepairable journal after the checks above, and a rollback (R0, R1, R2),
which booted the new code out first and hands the hold back to the old code.

## A legacy stop hold with failure receipts

Adoption keeps only a completed legacy stop's hold: phase `stopped`, with
every unsettled receipt one it can settle. Any other paused journal refuses
("holds a pause that is not a completed stop's maintenance hold", naming the
receipts it cannot settle and why). Read the receipts with the old
`ava maintenance status`.

**On a `stopped` hold no receipt is lost work.** Every runner receives every
wake, and the old host's stop cancelled wakes before their row read, latching
each as a `CancelledError` receipt of its own hold (FC-10 F20). Two rules of
the old code place every unsettled receipt of a `stopped` hold after its
certified drain: `shared/maintenance.py::set_phase` enters `drained` only with
no unsettled failure, and nothing else moves a hold past `draining`;
`shared/maintenance.py::record_drained` refuses a drained receipt once the
agent has a failure. From then on a wake of a drained or parked member claims
nothing (its inbound messages stay pending for the resume), and an agent
outside the cohort (`commands` and `parked`, every non-terminated agent of
the machine) has no continuation in the hold at all. The adoption's `hold`
step classifies the receipts and settles:

- **foreign** ones (outside the cohort): always;
- **drained** and **parked** members' ones: only when the home's legacy code
  carries those rules, which hold from `cc5c5e2098` (#1872) on. The home's
  `installed_sha` must name a descendant of it (`git merge-base
  --is-ancestor` in the owning checkout; the inventory reports the check as
  `pause_owner.legacy_rules`). Without that proof (no readable
  `installed_sha`, an unrelated commit, no repository) only foreign ones
  settle, and the refusal names the rest with the reason;
- a member neither drained nor reaped (`other`) contradicts a certified drain
  and refuses.

It records them in its journal as one `hold-disregard-receipts` effect: the
hold identity, `cohort`, `parked`, `drained`, the receipts by class, and the
evidence (for drained and parked ones the legacy commit, the rules commit and
both rules with their file and function). Then it moves exactly those
receipts into the hold's `repaired` with a `repair_record` naming the
adoption, refusing if they changed since the plan or a repair record already
stands. Copy the effect into the cutover record. The held first start and
`--resume` then see no unsettled receipt.

A hold with receipts that is not `stopped` has no adoption exit. Handle it
with the old code, before the host's code switch:

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
- **Known gap:** a hold past `drained` but not `stopped`, with a receipt: an
  old-code start ran after the stop and refused (phase `starting`, FC-10's LX
  hold; the W2 window rule forbids that start), or the stop never finished
  (phase `stopping`). The old code has no sanctioned exit: `repair` and
  `resume --cancel` refuse a started stop, and start and resume refuse
  unsettled receipts; the adoption keeps only a `stopped` hold. Never edit
  the journal by hand. Record the hold and its receipts, then exclude that
  runner from the window (it stays on the old code, stopped, and
  `--exclude-unit` at W6 keeps it fenced), or treat it as a no-go on the
  gateway (R1). The new code latches none of these receipts once its drain is
  certified ([receipts need a live
  continuation](../shared/maintenance/cohort-receipts.ava.okf.md)).

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

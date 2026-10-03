# TCC helper onboarding (one-pass grants)

How to get every AvaPermissionsHelper TCC grant in place in one sitting -- and
why it must happen before the helper-spawn backend is enabled on a host.

The grants a helper needs (Desktop / Documents / Downloads folder rows, the
AppleEvents target rows, Screen Recording, Accessibility) used to be met one at
a time, as individual workflows tripped over them -- a folder here, a dialog
there, spread over days, waking the user repeatedly ("whack-a-mole"). This
procedure front-loads all of them into a single user-at-the-machine session:
inventory what is missing with side-effect-free preflight queries, trigger each
remaining request in sequence, let the user click through, and finish with a
verdict matrix.

## Why before the flip

`AVA_PERMISSIONS_HELPER_SPAWN` (host scope, default off) routes service,
agent, and PTY-host spawns through the helper so their TCC attribution lands on
`com.ava.permissions-helper`. Enabling it while a grant is still missing flips
that access from the already-granted python identity to an ungranted helper
identity: dialogs at best, silent denials at worst. Complete this onboarding
first (per host), then flip.

## The tool

`scripts/tcc-onboard-helper-grants.py`, run on the host with the repository
venv and the user present:

```bash
.venv/bin/python scripts/tcc-onboard-helper-grants.py --check    # inventory only, zero dialogs
.venv/bin/python scripts/tcc-onboard-helper-grants.py            # interactive: trigger missing grants
.venv/bin/python scripts/tcc-onboard-helper-grants.py --tier L2  # the machine target tier (design v1)
.venv/bin/python scripts/tcc-onboard-helper-grants.py --items folders --timeout 180
.venv/bin/python scripts/tcc-onboard-helper-grants.py --fill-pending --confirm-user-present
                                                                 # + extended-group fills (experimental)
```

- Every trigger is a child process spawned through the helper
  (`services.permissions_helper.client.spawn_process`), so tccd attributes the
  request to `com.ava.permissions-helper`. The inventory probe uses the same
  helper-spawned `TCCAccessPreflight` pattern as
  `scripts/tcc-verify-spawn-chain.sh`: zero dialogs, repeatable.
- The interactive run waits for each dialog decision (default 90s per item,
  overridable with `--timeout`), skips items that are already granted, and ends
  with a summary plus a JSON report under the workdir (default
  `/tmp/tcc-onboard-helper-grants`); exit code 0 only when every requested
  item is granted/verified (1 = items unresolved, 2 = setup failure -- not
  macOS, unknown `--items`, helper unreachable, old helper build, probe
  timeout). A dialog that times out has its waiting child reaped
  (SIGTERM/SIGKILL) so nothing stays pending past the run.
- AppleEvents rows are keyed per target app and are granted only by a live
  dialog, so they are triggered one by one with the user watching; `--check`
  can not read them (there is no silent query for that row shape).
- Screen Recording and Accessibility can not be requested programmatically;
  the tool verifies them from the helper ping and points at System Settings
  when either is missing.
- Extended groups (`appdata`, `media`, `icloud`, `fda`, `devtools`) have
  their grant state read by the same preflight probe; `--fill-pending
  --confirm-user-present` adds best-effort triggers for `appdata` / `media` /
  `icloud` (experimental -- evidence levels in the trigger-method archive
  below). `fda` and `devtools` are never attempted, by decision.

The panel (`AvaPermissionsHelper.app`, shown on a socket-less launch) drives
the same tool: *Check status* runs `--check`; *Fill missing grants* runs it
with `--fill-pending --confirm-user-present` -- the click is the user-present
attestation, and there is no automatic fill path (a launch only checks). A
panel fill raises the same extended-group dialogs as the CLI, `icloud`
included: its prompt writes no `PROMPTING` line, so verify it by screenshot
(see the archive below).

## Tiers (design v1)

The machine-facing contract: a target tier names the authorization set a host
should hold. User decisions 2026-09-17: the full tier includes Full Disk
Access, and macmini's target is L2.

| Tier | Contents |
|---|---|
| L0 silent | No grants targeted; the tool refuses to probe or trigger (maintenance windows). |
| L1 standard | Folder rows x3 + AppleEvents x5 + Screen Recording / Accessibility (the pre-tier set). |
| L2 extended | L1 + other-app data + media library + iCloud surface. |
| L3 full | L2 + Full Disk Access (the heavy item) + DeveloperTool + future items. |

Groups beyond the triggerable set (`appdata`, `media`, `icloud`, `fda`,
`devtools`) always have their grant state read by the same preflight probe;
without `--fill-pending` they are never attempted, and a non-granted state is
reported as unresolved with the observed values. `--fill-pending` (guarded by
its required `--confirm-user-present`) adds a best-effort trigger for
`appdata` / `media` / `icloud` that still lack their grant -- each method's
evidence level is in the trigger-method archive below. `fda` and `devtools`
are never attempted, by decision.

## Trigger mechanics worth knowing

- **Documents can not be triggered through the helper file APIs.** The
  helper's `file_list` / `file_read` path whitelist covers only `~/Desktop`,
  `~/Downloads` and `~/.ava/incoming`; asking for `~/Documents` fails with
  `outside whitelist` before tccd sees a request (and a wrapper that catches
  exceptions as "request-logged" will misreport it as registered). The tool
  therefore triggers every folder through a spawned child that directly
  accesses the directory (`os.listdir`), which works uniformly for all three
  folder services.
- **Never use a Desktop-listing probe to verify.** A listing blocks on a
  permission prompt whenever the grant is missing, and a pending prompt blocks
  synthesized input machine-wide until a human clicks it. Verification is
  preflight-only; see `scripts/tcc-verify-spawn-chain.sh` for the pattern.
- **A helper build without the nursery `spawn` wire method can not be
  onboarded.** Older builds answer `unknown method: spawn`; rebuild the helper
  first (same signing identity), then run this tool.
- **Extended-group states are preflight-readable.** The five beyond-set
  services (SystemPolicyAppData, MediaLibrary + Photos, FileProviderDomain +
  Ubiquity, SystemPolicyAllFiles, DeveloperTool) answer `TCCAccessPreflight`
  silently; the 2026-09-17 validation also showed the readings discriminate
  (bogus service name -> denied; Microphone -> not-determined; FDA -> denied),
  so a granted reading is meaningful; the trigger side is the archive below.
- **Multiple helper instances on one host share the same TCC identity.** A
  second cluster instance (its own build, launchd service and socket -- e.g. a
  dev/test instance) needs no separate onboarding when its helper is built
  from the same signing identity: TCC grants are keyed to the bundle
  identifier plus certificate, so every instance carrying the same designated
  requirement inherits the same grants. Observed on macmini 2026-09-14: a
  dev-instance helper came up alongside the main one with no prompts. Only a
  new signing identity requires a fresh onboarding pass.

## Extended-group trigger methods (archive)

`--fill-pending` is EXPERIMENTAL: every method below carries its evidence
level, and an unresolved group after a fill attempt means "the method needs
another look on a machine that still lacks the grant", not "the grant is
impossible". Run it only with the user at the machine (a pending dialog
blocks synthesized input machine-wide until answered), and read the verdict
from the preflight re-read, not from the child's own output.

### appdata -- `kTCCServiceSystemPolicyAppData`

- Method: a helper-spawned child makes a bounded scan of
  `~/Library/Application Support` (listings plus small sample reads, capped in
  depth and entries). The prompt is raised indirectly: a process reaching into
  another app's container makes `sandboxd` relay a
  `TCCAccessRequestIndirectWithOptions` request (the sender is a sandboxd
  instance, not the touching process).
- Evidence (macmini, 2026-09-14): mechanism observed end to end -- four
  prompts, each later attributed to a process scanning the home directory
  (`find`, `du`, a recursive glob). Prompts DO write `AUTHREQ_PROMPTING` +
  `AUTHREQ_SUBJECT` (subject = the helper), but no `RESULT` row.
- Rebuild gotcha: unlike the DR-held rows, this row does NOT survive a
  same-identity helper rebuild -- it returns to `not-determined` and the next
  scan re-prompts (observed 2026-09-18: the routine update wave rebuilt the
  helper, the next scan re-prompted, and the user re-allowed the row the same
  day). Expect to re-run the fill after every helper rebuild.
- The fill path replays the evidenced surface; a deliberate fill of a missing
  row has not yet been exercised (macmini's row reads granted).

### media -- `kTCCServiceMediaLibrary` + `kTCCServicePhotos`

- Method (candidate): a helper-spawned child scans `~/Music` (MediaLibrary)
  and `~/Pictures/Photos Library.photoslibrary` (Photos); a missing library
  path is reported as such (a machine where Photos was never opened has no
  library to touch).
- Evidence: none yet -- macmini's rows read granted before a first-use test
  could run (preflight, 2026-09-17), so the first fill against a machine that
  still lacks the grants is the verification.
- The group resolves only when both services read granted in the recheck.

### icloud -- `kTCCServiceFileProviderDomain` + `kTCCServiceUbiquity`

- Method (candidate): a helper-spawned child scans
  `~/Library/Mobile Documents/com~apple~CloudDocs` (the FileProviderDomain
  surface).
- Blind spot: FileProviderDomain prompts do NOT write `AUTHREQ_PROMPTING`
  (observed 2026-09-14) -- log monitoring misses them; verify by screenshot,
  not logs.
- Evidence: none yet; same position as media. No distinct trigger surface is
  known for `kTCCServiceUbiquity`; the group status rechecks both services so
  a Ubiquity-only gap stays visible as unresolved.

### fda -- `kTCCServiceSystemPolicyAllFiles`

- Never triggered by this tool, by decision: macmini's target tier is L2 (no
  Full Disk Access), and an experimental prompt would leave a denied row on a
  machine that does not need the grant. The 2026-09-12 audit found no
  evidence the grant is needed; the user placed it in the full tier
  (2026-09-17), and the method is archived for a future L3 machine's first
  grant instead.

### devtools -- `kTCCServiceDeveloperTool`

- Never triggered by this tool, by decision: no current workflow needs
  DeveloperTool, and exercising it (debugger-attach class) would accrue a
  denied row without a use. L3-only; verify on a machine that actually needs
  it.

## Per-machine requirements (state as of 2026-09-14; first audit 2026-09-12)

| Machine | Helper | Folder rows | SR / AX | Notes |
|---|---|---|---|---|
| macmini | running, spawn wire OK (rebuilt 2026-09-13, same signing identity) | Desktop/Documents/Downloads granted | granted | Onboarded 2026-09-12; spawn backend enabled + verified. Rebuild 2026-09-13 kept every grant (stable identity); re-verified 2026-09-14: spawn-chain PASS, preflight matrix green. Target tier: L2 (user 2026-09-17). Extended states (appdata/media/icloud) read granted 2026-09-19 (preflight; the AppData row was reset by the 2026-09-18 rebuild and re-allowed the same day -- see the trigger-method archive). |
| company-mini | running, build predates `spawn` | to onboard after rebuild | granted | Rebuild first, same signing identity (an identity change silently drops the Accessibility grant) |
| macbook-air | running, build predates `spawn` | to onboard after rebuild | granted | Same as company-mini |
| company-air | not installed | all first-time | first-time | Fresh install + sign + first grants in one user-present session |

The owning evidence for the inventory side: the four-machine audit matrix
recorded with task #3202 (workspace `3202-tcc-audit/summary.md`).

## Interface with the migration matrix (#3195 P7)

Per-machine rebuild/reinstall flow gains one step, inserted before the flip:

1. Rebuild / reinstall the helper (design: stable signing identity, pinned
   designated requirement -- a rebuild that changes identity invalidates every
   existing grant and the onboarding must be redone).
2. Run the inventory: `.venv/bin/python scripts/tcc-onboard-helper-grants.py --check`.
3. Complete the missing grants with the user present: run the tool without
   `--check`; add `--fill-pending --confirm-user-present` to also attempt the
   extended groups (`appdata` / `media` / `icloud`) that still lack their
   grant (experimental methods -- see the archive).
4. Flip the backend: `ava config set permissions_helper_spawn=true --machine
   <host>` (official config API), restart the host's spawn-related services,
   then verify with `scripts/tcc-verify-spawn-chain.sh` (PASS = the probe's
   requests attributed to the helper; the per-service preflight results it
   prints are informational).

A fresh machine (no helper assets, e.g. company-air) starts one step earlier:
build + sign + launchd registration, then the same 2-4 with the user present
for the first grants.

## Troubleshooting

- `outside whitelist` -- a helper file API refused the path (see above); it is
  not a TCC state.
- `unknown method: spawn` -- helper build is older than the nursery spawn
  protocol; rebuild.
- A dialog was declined -- the row becomes denied; re-run the tool after
  granting in System Settings > Privacy & Security (the tool skips items that
  are already granted and reports the rest).
- Monitoring prompt traffic in `log show`: the query's own command line is
  echoed back into the results (`log run noninteractively`, containing the
  predicate text). Filter on `[com.apple.TCC:access]` to exclude the echo, or
  a monitoring loop will flag its own queries as events.

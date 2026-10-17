# TCC helper onboarding (one-pass grants)

How to get every MeivaPermissionsHelper TCC grant in place in one sitting -- and
why it must happen before the helper-spawn backend is enabled on a host.

The grants a helper needs (Desktop / Documents / Downloads folder rows, the
AppleEvents target rows, Screen Recording, Accessibility) used to be met one at
a time, as individual workflows tripped over them -- a folder here, a dialog
there, spread over days, waking the user repeatedly ("whack-a-mole"). This
procedure front-loads all of them into a single user-at-the-machine session:
inventory what is missing with side-effect-free preflight queries, trigger each
remaining request in sequence, let the user click through, and finish with a
verdict matrix.

## First-run gate (the product path)

The one-pass tool above is an operator command: someone has to know to run it.
The first-run gate is the product's own answer, so a host never discovers a
missing grant in the middle of a task.

- **One catalog.** `base/host/permissions_helper/grants/catalog.py` describes every
  authorization Meiva can need: which process holds it, what it unlocks, whether
  a dialog can still grant it, which System Settings pane holds it, and how to
  confirm it. The CLI, the converge step, the gateway endpoint and the console
  section all read it; no other file spells a TCC service or a "why" line.
- **One probe.** `base/host/permissions_helper/grants/probe.py` reads the matrix through a
  helper-spawned child (`children/preflight.py`). `--check` and the gate call the
  same function, so the tool and the product cannot disagree about a host.
- **One state machine.** `base/host/permissions_helper/grants/gate.py` turns one probe into
  `complete` / `action_required` / `deferred` / `unavailable` /
  `not_applicable` plus the per-item evidence. `denied` and `not_determined`
  block completion; `unknown` (an Automation row nothing has attempted yet) does
  not. A deferred host stays in the restricted family -- "later" is recorded, it
  is never a grant.
- **One surface.** `meiva permissions status` prints the checklist and exits 1
  while anything is missing; `meiva permissions onboard` raises the dialogs a
  person can still answer and then re-probes; `meiva permissions defer` records
  "later"; `meiva permissions restart-helper` restarts the helper so a fresh
  Screen Recording grant is re-read. The console renders the same rows at
  Control -> Permissions (`GET /api/host/permissions`) and keeps a banner up
  while `state` is restricted.

Rules the implementation keeps:

- **A state always comes from a fresh probe.** The record under
  `$MEIVA_HOME/permissions_onboarding.json` is the last observation plus the
  user's "later" flag; it is written from a probe and never read as evidence.
- **No dialog without a human.** `onboard` refuses to request unless its caller
  says a person is at the machine (`--confirm-user-present` on the tool, the
  button click on the API, a TTY on the CLI/converge). A pending dialog blocks
  synthesized input machine-wide until it is answered.
- **A request is not its own evidence.** After requesting, the gate re-probes;
  a child that reports "granted" while the system still denies stays denied.
- **First converge asks, later converges remind.** The converge step requests
  only when no record exists yet (the install-time case) and prints the checklist
  then; afterwards it re-reads and points at `meiva permissions status`.

## Why before the flip

`MEIVA_PERMISSIONS_HELPER_SPAWN` (host scope, default off) routes service,
agent, and pty-sessions service spawns through the helper so their TCC attribution lands on
`com.meiva.permissions-helper`. Enabling it while a grant is still missing flips
that access from the already-granted python identity to an ungranted helper
identity: dialogs at best, silent denials at worst. Complete this onboarding
first (per host), then flip.

## The tool

`scripts/host_ops/tcc/onboard-helper-grants.py`, run on the host with the repository
venv and the user present:

```bash
.venv/bin/python scripts/host_ops/tcc/onboard-helper-grants.py --check    # inventory only, zero dialogs
.venv/bin/python scripts/host_ops/tcc/onboard-helper-grants.py            # interactive: trigger missing grants
.venv/bin/python scripts/host_ops/tcc/onboard-helper-grants.py --tier L2  # the machine target tier (design v1)
.venv/bin/python scripts/host_ops/tcc/onboard-helper-grants.py --items folders --timeout 180
.venv/bin/python scripts/host_ops/tcc/onboard-helper-grants.py --fill-pending --confirm-user-present
                                                                 # + extended-group fills (experimental)
```

- Every trigger is a child process spawned through the helper
  (`base.host.permissions_helper.client.spawn_process`), so tccd attributes the
  request to `com.meiva.permissions-helper`. The inventory probe uses the same
  helper-spawned `TCCAccessPreflight` pattern as
  `scripts/host_ops/tcc/verify-spawn-chain.sh`: zero dialogs, repeatable.
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

The panel (`MeivaPermissionsHelper.app`, shown on a socket-less launch) drives
the same tool: *Check status* runs `--check`; *Fill missing grants* runs it
with `--fill-pending --confirm-user-present` -- the click is the user-present
attestation, and there is no automatic fill path (a launch only checks). A
panel fill raises the same extended-group dialogs as the CLI, `icloud`
included: its prompt writes no `PROMPTING` line, so verify it by screenshot
(see the archive below).


## The full matrix

| Authorization | TCC service / row | Holder | First request today | Programmatic request | Manual only | Helper restart |
|---|---|---|---|---|---|---|
| Screen Recording (incl. ScreenCaptureKit) | `kTCCServiceScreenCapture` | helper | helper start (`registerPermissions`) + first capture | one-time prompt at helper start (`CGRequestScreenCaptureAccess`) | **yes** once denied -- System Settings > Screen Recording | **yes** |
| Accessibility | `kTCCServiceAccessibility` | helper | helper start + first click/keystroke | one-time prompt at helper start (`AXIsProcessTrustedWithOptions`) | **yes** once denied | no (read live per call) |
| Desktop / Documents / Downloads folders | `kTCCServiceSystemPolicy{Desktop,Documents,Downloads}Folder` | helper | first folder operation by whichever workflow needed it | yes -- helper-spawned child lists the folder | after a denial | no |
| Automation (Finder, Terminal, System Events, Safari, Chrome) | `kTCCServiceAppleEvents` per target | helper | first AppleEvent to that app | yes -- one benign `get version` event per app | after a denial | no |
| App data | `kTCCServiceSystemPolicyAppData` | helper | -- (no shipped feature uses it) | experimental (`--fill-pending`) | -- | no |
| Media & Apple Music / Photos | `kTCCServiceMediaLibrary`, `kTCCServicePhotos` | helper | -- | experimental (`--fill-pending`) | -- | no |
| iCloud Drive | `kTCCServiceFileProviderDomain`, `kTCCServiceUbiquity` | helper | -- | experimental (`--fill-pending`) | -- | no |
| Full Disk Access | `kTCCServiceSystemPolicyAllFiles` | helper | never requested | no (read-only by decision) | always | no |
| Developer Tools | `kTCCServiceDeveloperTool` | helper | never requested | no (read-only by decision) | always | no |
| Notifications | -- (Tauri app bundle) | app | Android only (`request_notification_permission_async`) | n/a on macOS today | -- | no |
| Camera / Microphone | -- | -- | not used by any shipped feature | -- | -- | -- |

Reading the last three rows: Meiva raises no macOS notification authorization
today (in-product notices are web UI), and nothing captures camera or
microphone; `scripts/host_ops/tcc/preauth.sh` reports them only because it
inventories the whole TCC database.

## Tiers (design v1)

The machine-facing contract: a target tier names the authorization set a host
should hold. The full tier includes Full Disk Access; the deployment owner
chooses the target tier for each authorized host.

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
  `~/Downloads` and `~/.meiva/incoming`; asking for `~/Documents` fails with
  `outside whitelist` before tccd sees a request (and a wrapper that catches
  exceptions as "request-logged" will misreport it as registered). The tool
  therefore triggers every folder through a spawned child that directly
  accesses the directory (`os.listdir`), which works uniformly for all three
  folder services.
- **Never use a Desktop-listing probe to verify.** A listing blocks on a
  permission prompt whenever the grant is missing, and a pending prompt blocks
  synthesized input machine-wide until a human clicks it. Verification is
  preflight-only; see `scripts/host_ops/tcc/verify-spawn-chain.sh` for the pattern.
- **A helper build without the nursery `spawn` wire method can not be
  onboarded.** Older builds answer `unknown method: spawn`; rebuild the helper
  first (same signing identity), then run this tool.
- **AppleEvents rows need the helper's AppleEvents entitlement.** The helper
  is signed with `com.apple.security.automation.apple-events` (input:
  `services/desktop/permissions_helper/helper/helper.entitlements`), and only then
  can tccd build an attribution chain for the spawned `osascript` child:
  each target reaches a normal Automation dialog, one at a time. Without it
  the request dies silently (`-1712` / `-609`) -- if the rows never prompt,
  check the installed helper's entitlements (`codesign -d --entitlements :-`)
  before blaming the row.
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

- Full Disk Access belongs to L3. Do not trigger its experimental prompt on
  an authorized host whose chosen target is L2: it would add a denied row
  without a required capability. Use the archived method only when the
  deployment owner explicitly authorizes L3 onboarding for that target.

### devtools -- `kTCCServiceDeveloperTool`

- Never triggered by this tool, by decision: no current workflow needs
  DeveloperTool, and exercising it (debugger-attach class) would accrue a
  denied row without a use. L3-only; verify on a machine that actually needs
  it.

## Deployment inventory

Each deployment keeps its own machine roster and current grant state outside
this contributor guide. Before onboarding a unit, inspect its helper build,
required access tier and folder/screen/accessibility grants with the user
present. A historical grant on another machine is not evidence for this unit.

## Interface with the migration matrix (#3195 P7)

Per-machine rebuild/reinstall flow gains one step, inserted before the flip:

1. Rebuild / reinstall the helper (design: stable signing identity, pinned
   designated requirement -- a rebuild that changes identity invalidates every
   existing grant and the onboarding must be redone).
2. Run the inventory: `.venv/bin/python scripts/host_ops/tcc/onboard-helper-grants.py --check`.
3. Complete the missing grants with the user present: run the tool without
   `--check`; add `--fill-pending --confirm-user-present` to also attempt the
   extended groups (`appdata` / `media` / `icloud`) that still lack their
   grant (experimental methods -- see the archive).
4. Flip the backend: `meiva config set permissions_helper_spawn=true --machine
   <host>` (official config API), restart the host's spawn-related services,
   then verify with `scripts/host_ops/tcc/verify-spawn-chain.sh` (PASS = the probe's
   requests attributed to the helper; the per-service preflight results it
   prints are informational).

A fresh authorized host with no helper assets starts one step earlier:
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

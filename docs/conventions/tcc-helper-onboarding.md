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
.venv/bin/python scripts/tcc-onboard-helper-grants.py --items folders --timeout 180
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
- **Full Disk Access is out of scope.** The audit found no evidence it is
  needed; the tool ignores it.

## Per-machine requirements (state as of 2026-09-12)

| Machine | Helper | Folder rows | SR / AX | Notes |
|---|---|---|---|---|
| macmini | running, spawn wire OK | Desktop/Documents/Downloads granted | granted | Onboarded 2026-09-12; spawn backend enabled + verified (probe attributed to the helper) |
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
   `--check`.
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

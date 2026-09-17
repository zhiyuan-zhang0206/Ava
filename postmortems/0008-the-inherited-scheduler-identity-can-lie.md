# 0008 — The inherited scheduler identity can lie

**Date:** 2026-09-17
**Anchors:** task #3791, `shared/os_cron.py::_register_macos`,
`shared/platform.py` (`descends_from_launchd_job` / `launchd_job_loaded`),
`tests/shared/test_os_cron.py`, `tests/shared/test_watchdog_launchd_reload.py`;
predecessor guard `aa9c509c6` and
[`postmortems/0005`](0005-a-supervisor-cannot-replace-itself.md); evidence host:
company-air preview home, macOS 26.6.2 (25G83).

## Summary

A health probe's automatic rollback ran `ava start` beneath its own launchd
job, registration booted that job out, and the recovery tree died mid-flight —
leaving the host paused with its deploy lease orphaned. This is the 0005 class
a second time, but the guard added then never fired: it keyed on the inherited
`XPC_SERVICE_NAME`, and on current macOS that variable only reaches the job's
DIRECT child — every exec'd descendant reads `"0"`, which is exactly where
converges run. Registration now proves ownership against the scheduler's own
view (the job's live pid inside this process's ancestry); the environment
comparison remains only a fast path.

## Timeline

2026-09-17, preview cluster on company-air (CST):

- 15:18: source advanced by hand for a pre-window check; the dev-cluster source
  guard reverted it on the next cycle, so the probe's checks began failing (by
  design) and the consecutive-failure counter started.
- 15:30:26: failure count reached threshold 3. The probe process (launchd job
  `...health-probe`, pid `py41817`) spawned `ava cluster rollback --yes`
  (`py41820`); the rollback took the deploy lease, paused and drained the
  agent-runner.
- 15:30:28: checkout back to the installed sha; `uv sync`; then
  `ava start --persist-services` (`py41844`) — whose service ports answered
  EADDRINUSE from the still-running old processes.
- 15:30:34.405/.412/.419: converge reached the OS-job register steps and issued
  `launchctl bootout` for logs-maintenance, packages-refresh, and finally
  health-probe — the last one from inside that job's own process tree.
  launchd's log: `health-probe [41817]: bootout initiated by:
  launchctl[41973]<-python3.12[41844]<-python3.12[41820]<-python3.12[41817]`.
  launchd SIGTERMed the job (ran 7.8 s); the rollback's `finally` (resume +
  lease release) never ran.
- 15:30:46: the deploy watchdog reclaimed the dead lease, but the pause had no
  owner left: ops rounds blocked and `cluster update` refused for ~9 minutes.
- 15:39: the documented recovery path (`ava start`) restored the host in ~30 s.

## Root cause

`shared/os_cron._register_macos` compares `launchd_job_label()` (=
`os.environ["XPC_SERVICE_NAME"]`) with the labels this home owns. The 0005 fix
was built on "launchd injects the label into the job and its descendants" — and
on macOS 26.6.2 that is false in the descendant direction: three disposable
LaunchAgent experiments on the affected host (direct python; bash -> python ->
bash; with and without the plist environment block) show the direct job process
reads its label, while EVERY exec'd child reads `"0"`. The rollback's
grandchild `ava start` therefore read `"0"` — not `None`, not the label — fell
straight through the guard, and booted out the job.

Escape analysis:

- The 0005 regression tests model launchd at the subprocess boundary
  (monkeypatch the env var). They pinned the guard's LOGIC while sharing its
  wrong premise; the OS contract itself was never validated against a live
  agent — 0005's own text said so ("remains an operating-system assumption").
- The site of the damage is an auto-rollback under a launchd job, the least
  reproducible environment in the fleet; nothing exercised the guard there.
- The EADDRINUSE trouble on the restart leg was visible but non-fatal; the
  actual killer surfaced only through unified-log forensics (launchd's
  `bootout initiated by:` lines name the whole process chain).

## Guardrails added

- `shared.platform.descends_from_launchd_job(label)`: asks `launchctl print`
  for the job's live pid and walks this process's ancestry with `ps`;
  `launchd_job_loaded` shares the loaded-verdict helper. Proven ownership
  defers; everything else proceeds as before.
- `shared/os_cron._register_macos` and
  `shared/os_watchdog_probe._register_macos` (the repo-wide call sites of
  `launchd_job_label`, verified by grep) defer on proven ancestry; the env
  comparison stays as a cheap fast path, never the proof.
- Tests: `test_register_macos_defers_when_ancestry_proves_the_job` (env reads
  `"0"`, ancestry proves self -> defer; the watchdog suite pins its twin) plus
  siblings — env-zero external reload still proceeds, and platform-level walk
  semantics (not loaded / not running / unreadable ancestor -> not proof).
  Red battery: with the pre-fix source restored, the three new ownership guards
  fail (the env-only guard falls through to bootout).
- Validated on the host itself BEFORE shipping: a disposable LaunchAgent
  reproduced the env behaviour and the pid-walk detected ownership
  (SELF-FOUND), so the fix's mechanism is observed, not assumed.

Left open: the restart leg still does not stop services before `ava start`, so
EADDRINUSE on the old ports can recur (drain is not stop; that area belongs to
the stop-orchestration work, task #3591). No CI runs a real launchd lifecycle —
the live-host experiment remains the evidence for the OS contract.

## Lessons

- An inherited environment variable is a hint, not an identity. Verify process
  ownership against the scheduler's own view of the live process, not against
  what the process believes it inherited.
- A guard keyed on an unvalidated platform contract is a guard in name only.
  When CI cannot exercise the contract, reproduce the mechanism on the real
  host with a disposable experiment before trusting the fix.
- A source comment must not promise more than the code knows: "launchd injects
  ... into the job and its descendants" was the assumption that hid the hole.

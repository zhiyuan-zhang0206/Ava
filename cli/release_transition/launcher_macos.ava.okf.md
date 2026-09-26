---
type: doc
title: macOS release executor custody
description: One finite launchd job per attempt runs the hardened, stably signed helper's finite mode; fail-closed readback, group-scoped closure proven for every terminal, exact-label retirement and boot/login recovery.
tags: [cluster-lifecycle, release, macos]
---

# macOS release executor custody

`launcher_macos.py` is the macOS native adapter selected by `native.py`, the
one dispatch point: a recorded launch is read back, retired and continued by
the adapter of its recorded `kind`, which every launch plan writes and which
every read or write of the journal requires; a missing or unrecognized kind is
a fail-fast refusal, never a systemd default. A new launch uses the host's
adapter or refuses. It owns only the finite executor. The persistent home
helper keeps owning ava-root. Records and job-group closure live in
`launchd_custody.py`.

## Launch

Each attempt is one LaunchAgent in `gui/<uid>`, labelled
`com.ava.release-executor.<sha256(operation path)[:32]>.a<attempt>`. Its private
plist lives in the operation directory (`executor/a<attempt>/`), never in
`LaunchAgents`: `RunAtLoad`, `KeepAlive=false`, `AbandonProcessGroup=false`,
`ExitTimeOut` 20, umask 022, fixed argv, private logs, no
`EnvironmentVariables`. `LaunchOnlyOnce` is excluded: measured on macOS 26 it
drops the job after exit, destroying terminal evidence.

The job runs `AvaPermissionsHelper --finite-executor v1 --cwd DIR
--group-receipt PATH --env K=V ... -- ARGV` of the same stably signed artifact
the live home helper runs (`services/permissions_helper/finite_artifact.py`):
the helper is the kernel's socket peer (`LOCAL_PEERPID`, never the PID it
reports) with `finite_executor_v1`, a launchd job of this user whose running
image satisfies the stable requirement; the binary is this home's installed
bundle (`home/helper` or the configured artifact dir) in canonical owner-only
directories; its sha256 and `codesign --verify --strict -R=<stable
requirement>` describe one unchanged file (inode and ctime bracket both).
`codesign --verify` without `-R` accepts an ad-hoc bundle that merely embeds
the stable requirement text. A continuation must use the identical artifact.

The helper is signed with the hardened runtime, so dyld ignores `DYLD_*` (for
example from `launchctl setenv`). Admission requires it on the file and in the
running image's kernel status (`csops`: valid and `CS_RUNTIME`) of the home
helper and every finite helper readback: `codesign -R <pid>` alone passes for a
process whose inserted library ran before `main`.

The finite mode (`helper/main.swift`, entered before any desktop setup) first
re-executes itself with an empty environment (hygiene only: it runs after
dyld), requires being a launchd job process-group leader, registers its
TERM/INT/HUP sources before ignoring them, publishes the
group receipt (helper PID = PGID, audit session) by exclusive rename, then
spawns exactly one executor without SETSID/SETPGROUP with envp only from
`--env`. After reaping the executor it closes its own group while it still
leads it: SIGTERM, 5 s grace, SIGKILL to each remaining member. Diagnostic log
writes never abort it. Exit codes: 0 / 80 (executor failed) / 81 (executor
signalled) and its own 64, 65, 70, 71, 73, 75, 82, 83 (`FINITE_EXIT`).

`plan_launch()` has no effects. `launch()` holds the operation lock,
re-verifies image and helper, requires exact label absence and the program
file present with its digest (launchd holds a deferred spawn for a missing
program instead of failing), writes the plist (exclusive or identical bytes),
records the attempt, then runs `launchctl bootstrap`. A failed or lost response
retains the attempt; recovery is readback of that label only. The executor
computes and records its receipt under the operation lock, only for the launch
it was started for; `record_native` refuses a receipt of another attempt.

## Readback contract

`launchd_print.py` is an explicit, version-bound contract for `launchctl print`
(diagnostic output, not an API): supported product major 26, measured on
26.6.2 (25G83). It admits the measured structure only: unique known top-level
fields, known blocks, no deeper nesting, `LaunchAgent`, state `running` (one
PID, active count 1, never exited) or `not running` (no PID, active count 0,
exactly one exit code xor terminating signal, the signal only as Darwin's exact
`strsignal` text, e.g. `Trace/BPT trap: 5`). `spawn scheduled` is a pending
spawn: live custody, never terminal. Coalition counters are never read.
Anything else, a failed query or a timeout retains custody. Only `rc 113` with
the exact "Could not find service" text is absence of a current job.

Readback also requires the loaded definition to equal the journal (plist
path/bytes, program, arguments, cwd, logs, umask, exit timeout, runs 1, policy
`runatload | inferred program`), brackets observations with a second query,
checks the helper (ppid 1, own PGID, exact argv, executable, digest and the
running image's `codesign -R`) and its single direct child (PGID, argv, cwd).
A spawned executor requires the matching group receipt. While the executor
lives, any traceable descendant outside the job PGID refuses. A process
exiting mid-read is changed custody, never absence.

## Closure and retirement

launchd's own cleanup is one SIGTERM to the job group when the helper exits; a
member that ignores or handles it survives. Every terminal is therefore closed
only by proof: without a group receipt the helper spawned nothing; with one,
the recorded births must not be live and `killpg(pgid, 0)` must find the group
empty (a different live birth at the recorded helper's PID also proves it,
since XNU never allocates a live group's id). The executor spawns tools before
it can record a receipt, so this proof never depends on one. Survivors after a
bounded wait are SIGKILLed only while the recorded executor is alive in the
group (so its id cannot have been reused), by captured birth, executor last;
otherwise closure refuses, naming the members, and nothing is signalled: the
operator confirms and terminates them, then retries the same update command.
Closure is group-scoped: a descendant that created its own group or session
survives it. Hence only same-schema `Request`; `PitrRequest` refuses on macOS.

`retire_current()` records the terminal as deletion intent, re-observes the
identical terminal job, runs exact-label `bootout` and records retirement only
after positive absence. A job that reappears after recorded absence refuses.
When launchd holds no facts, the terminal names its evidence: `boot-changed`
(the recorded boot ended every process; the label must not be loaded now) or
`domain-lost` (same boot, the label exactly absent and `gui/<uid>` now under a
different audit session than the recorded one, owners gone and group empty).
Absence under the recorded session stays unknown custody. Settled history,
and an earlier boot, only re-prove that the label is not loaded, by launchd's
not-found codes rather than its wording. `resume()` retires, keeps
request/phase/direction, archives launch/native/terminal and dispatches the
next attempt label once.

## Current scope

darwin `for_host` admits a same-schema release `Request` (common release
preflight scope); root start goes through the persistent home helper:
[[cli/release_transition/root_macos.ava.okf.md]]. `PitrRequest` refuses before
reservation. Unit tests cover the parser, journal evidence, adapter paths and
real process groups. Opt-in native tests (`AVA_NATIVE_RELEASE_LAUNCHER=1`,
stable identity with `AVA_NATIVE_SIGNED_HELPER=1`) run disposable jobs for
natural exit, lost bootstrap response, concurrent launch, executor and helper
KILL, SIGTERM-ignoring members, SIGUSR1/2 terminals, the escaped-group control
and `DYLD_INSERT_LIBRARIES` injection (unhardened control refused, hardened
helper unaffected). Logout and reboot are unit-tested only.

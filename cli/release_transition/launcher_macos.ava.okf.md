---
type: doc
title: macOS release executor custody
description: One finite launchd job per attempt runs the signed helper's finite mode; fail-closed readback, group-scoped closure and exact-label retirement.
tags: [cluster-lifecycle, release, macos]
---

# macOS release executor custody

`launcher_macos.py` is the macOS native adapter selected by `native.py`, the
one dispatch point: a recorded launch is read back, retired and continued by
the adapter of its recorded `kind`; a new launch uses the host's adapter or
refuses. It owns only the finite executor. The persistent home helper keeps
owning ava-root, so neither root replacement nor finite-job retirement ends the
other lifetime. The operation journal and state machine are the Linux ones.

## Launch

Each attempt is one LaunchAgent in `gui/<uid>`, labelled
`com.ava.release-executor.<sha256(operation path)[:32]>.a<attempt>`. Its private
plist lives in the operation directory (`executor/a<attempt>/`), never in
`LaunchAgents`: `RunAtLoad`, `KeepAlive=false`, `AbandonProcessGroup=false`,
`ExitTimeOut` 20, umask 022, fixed argv, private logs, no
`EnvironmentVariables`. `LaunchOnlyOnce` is excluded: measured on macOS 26 it
drops the job after exit, destroying terminal evidence.

The job runs `AvaPermissionsHelper --finite-executor v1 --cwd DIR --env K=V ...
-- ARGV` of the same stably signed helper artifact the live home helper runs
(`services/permissions_helper/finite_artifact.py`: ping with
`finite_executor_v1`, executable of that live launchd job, sha256, strict
signature verify and the stable designated requirement). A continuation must
use the identical artifact; a helper upgrade is an external prerequisite.

The finite mode (`helper/main.swift`, entered before any desktop setup)
requires being a launchd job process-group leader, spawns exactly one executor
without SETSID/SETPGROUP, resets signal state, builds envp only from `--env`
(launchd adds session variables to the job itself), forwards TERM/INT/HUP
only to its unreaped child, reaps it under the forwarding lock and exits
0 / 80 (executor failed) / 81 (executor signalled) / 64, 65, 71, 75, 82 for
its own refusals (`FINITE_EXIT` mirrors the Swift codes).

`plan_launch()` has no effects. `launch()` holds the operation lock,
re-verifies image and helper, requires exact label absence, writes the plist
(exclusive or identical bytes), records the attempt, then runs `launchctl
bootstrap`. A failed or lost response retains the attempt; recovery is
readback of that label only. A bounded settle observes startup; the executor
itself records helper and executor births before any effect or child.

## Readback contract

`launchd_print.py` is an explicit, version-bound contract for `launchctl print`
(diagnostic output, not an API): supported product major 26, measured on
26.6.2 (25G83). It admits the measured structure only: unique known top-level
fields, known blocks, no deeper nesting, `LaunchAgent`, state `running` (one
PID, active count 1, never exited) or `not running` (no PID, active count 0,
exactly one exit code xor terminating signal). Coalition counters are never
read. Anything else, a failed query, a timeout or non-exact absence text
retains custody. Only `rc 113` with the exact "Could not find service" text
is absence.

Readback also requires the loaded definition to equal the journal (plist
path/bytes, program, arguments, cwd, logs, umask, exit timeout, runs 1, policy
`runatload | inferred program`), brackets observations with a second query,
checks the helper (ppid 1, own PGID, exact argv, executable and digest) and
its single direct child (PGID, argv, cwd). While the executor lives, any
traceable descendant outside the job PGID refuses. A process exiting mid-read
is changed custody, never absence.

## Closure and retirement

A terminal job is closed only when the recorded helper and executor births are
not live and the job process group is empty (bounded wait for launchd's group
cleanup). This is group-scoped: a descendant that created its own group or
session survives, and after its parent exits nothing here can see it. Hence
the admission scope: only same-schema `Request`; `PitrRequest` refuses before
effects on macOS. Without a recorded birth, the executor did no effect and
spawned nothing, so the terminal claims no closed births.

`retire_current()` records the terminal observation as deletion intent,
re-observes the identical terminal job, runs exact-label `bootout` and records
retirement only after positive absence. A job that reappears after recorded
absence refuses. Settled history in a later boot or OS build only re-proves
absence. `resume()` retires, keeps request/phase/direction, archives
launch/native/terminal and dispatches the next attempt label once.

## Current scope

darwin `for_host` admission refuses every request before reservation: the
macOS root-start branch through the persistent home helper (`root_service` /
`stage`) is not connected, so no operation can quiesce into an unconnected
start. Unit tests cover the parser, journal evidence and adapter paths.
Opt-in native tests (`AVA_NATIVE_RELEASE_LAUNCHER=1`, stable-identity
signing with `AVA_NATIVE_SIGNED_HELPER=1`) run disposable jobs for natural
exit, lost bootstrap response, concurrent launch, executor KILL with a
same-group child plus continuation, helper KILL, and the escaped-group
negative control. This is transport and custody evidence, not application
A/B/A.

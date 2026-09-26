---
type: doc
title: Linux release executor custody
description: Exact finite systemd execution, durable continuation and native unit retirement.
tags: [cluster-lifecycle, release, linux]
---

# Linux release executor custody

`launcher_linux.py` submits one finite executor to the system manager, the same
manager scope as `shared/os_boot_unit.py`. It does not own application services.
macOS requires its helper ancestry adapter; there is no direct-spawn or session
fallback here.

The existing operation journal at `home/updates/<uuid>/operation.json` carries
the exact `VerifiedRelease` interpreter, module argv, working directory,
home/registry, user, environment, boot identity and deterministic unit name.
The unit name includes the durable executor attempt number.
`plan_launch()` has no effects. The caller records that plan; `launch()` holds
the home operation lock, verifies the image again, records the one dispatch
attempt durably, and submits `systemd-run`. A failed or interrupted submission
retains its attempt. Recovery calls `readback()` against that same name; neither
missing evidence nor a removed unit authorizes another launch.

`resume()` may continue an incomplete operation only after a fresh native
readback proves that the exact prior invocation has finished with no remaining
cgroup members. The submitting controller calls `retire_current()` to persist
that terminal observation and deletion intent before asking systemd to stop
and, for a failed unit, reset the exact definition. Retirement requires fresh
matching invocation evidence and recursively empty cgroup both before effects
and when native absence is recorded. A missing unit before deletion intent
refuses; absence after that receipt finishes an interrupted retirement safely.
A live or changed unit cannot be retired. Completed submissions retain and
return their terminal receipt after deletion instead of requiring a vanished
unit to answer again. The finite executor cannot prove its own death.

A completed operation's recorded absence survives a host reboot. Replaying that
receipt revalidates its captured launch identity and checks that the unit and
cgroup remain absent; it cannot send stop/reset to an old-boot executor. A
reappeared unit or any unresolved prior attempt still refuses. This distinguishes
settled history from native custody that has not yet been closed.

Under the same operation lock, continuation requires completed retirement and
retains the old launch,
captured birth (when available), and complete terminal observation, increments
the attempt, records the next plan, and submits it once. The request, phase,
and release decision do not change. If the first executor exited before a
birth receipt was saved, its verified terminal invocation is retained without
inventing a process birth. Concurrent callers with the retired plan refuse;
completed operations cannot create another attempt.

The executor uses `Type=exec`, `Restart=no`, `RemainAfterExit=yes`, and
`KillMode=control-group`. No shell, inherited session, `--wait`, or `--collect`
is involved. Systemd starts the process in its own `/system.slice` cgroup. The
readback checks the native unit definition, structured D-Bus ExecStart and
environment, invocation identity, and kernel PID/start ticks, UID, parent, cwd,
argv and cgroup. A finished parent requires an empty cgroup, including nested
descendants (`cgroup.events`); exit status alone does not prove closure or a
successful release transition. The operation retains the first native identity
instead of replacing its birth receipt with a later exit observation.

Application startup goes through `BootStartAction` on the **existing home boot
unit**, with its existing Type=forking/ControlPID/PIDFile ownership checks. A
stage command runs there, so the new root is never a child of the finite
executor's cgroup. The operation then writes the steady pinned-image boot
action before completing. A one-operation stage action disables automatic
restart; it cannot become a permanent replay of that operation.

The initial native contract targets Linux systemd 255 with unified cgroups,
using the existing noninteractive system-manager privilege boundary. Unknown
manager state, unavailable native metadata, a reboot during unresolved custody,
or changed ownership retains unresolved custody. Only the captured finite executor unit can be
retired; application boot units and unknown processes remain outside this API.

Primary API contracts: [systemd-run v255](https://github.com/systemd/systemd/blob/v255/man/systemd-run.xml)
and [systemd D-Bus v255](https://github.com/systemd/systemd/blob/v255/man/org.freedesktop.systemd1.xml).
Unit tests cover journal-before-effect, retained uncertain dispatch, duplicate
refusal, changed native identity, wrong argv/cgroup, retained children, and
continuation without changing the release decision. The opt-in native fixture
also kills an exact executor through its systemd unit, continues it under a new
attempt, and proves the unrelated sibling cgroup survives. This is transport
and custody evidence, not a complete application A/B/A proof.

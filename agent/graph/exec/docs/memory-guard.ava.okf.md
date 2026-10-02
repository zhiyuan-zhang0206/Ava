---
type: doc
title: Exec Memory Guard — Critical Pressure Kills the Largest Exec
description: 'At critical system memory pressure the agent host kills the one exec process domain with the largest footprint, and that exec''s result tells its agent why.'
tags:
- agent
- exec
- memory
---

# Exec memory guard

A runaway `execute_code` can grow until the operating system reclaims memory from a process of its own choosing, the agent host included. The guard (`services/agent_host/exec_memory_guard.py`) is the agent host's answer: relieve the pressure with the smallest possible kill, and say so.

## Mechanism

- One sequential loop, the agent host background task `exec_memory_guard`, ticks every few seconds. Each tick reads the pressure level the OS itself reports (`base/host/memory_pressure.py`; macOS `kern.memorystatus_vm_pressure_level`). No memory threshold of ours is involved.
- Only at `critical` does it look at the exec process domains: the host's descendants that lead a session running an exec entry module, with the agent id and result path read from the leader's launch environment. It picks the one with the largest footprint (macOS `phys_footprint`, which counts compressed and swapped pages that `ps` RSS leaves out; a domain's footprint is the sum over its process group).
- It records the reason beside that run's result file (`base/native_process/exec_kill_notice.py`) and SIGKILLs the session leader. The owning run sees the root exit as for any external SIGKILL: its domain-close owner kills the rest of the group and reaps, then reads and consumes the notice.
- After a kill the loop sleeps one interval and re-reads the level. A recovered system is left alone; a system still critical loses the next largest exec, one at a time.

## What the agent sees

The crashed result carries the partial output plus a line stating that the host memory guard killed this exec at critical memory pressure, how large it was (GiB) and how many execs were running, with the advice to batch the work and stream or chunk reads. The kill emits the `exec_memory_guard_killed` event (agent id, footprint, running count, pressure).

## Platforms

macOS only. Linux exposes pressure as rates and counters (PSI, cgroup events), not a state the kernel declares critical, so `host_memory_source()` returns None there and the guard does not run.

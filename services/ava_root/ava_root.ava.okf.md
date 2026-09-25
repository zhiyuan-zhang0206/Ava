---
type: doc
title: Ava Root — one application service owner
description: Native process custody, immutable launch generations, and explicit platform lifetime adapters.
tags: [services, lifecycle]
---

# Ava Root

`ava-root` owns one home's application service tree. Ordinary start prepares a
validated manifest, launches the root, and requires fresh ownership-bound
protocol readiness. Repeated start reuses the same generation. Changed service
membership, commands, per-service environment, or the exact root launch
environment, or development source bytes requires an explicit prior stop; start cannot drain active work by
silently replacing its supervisor. The public start intent lock serializes the
whole preparation and readiness operation for that home. Admission runs before
converge, schema mutation, or desired service publication. A live identical root
skips those preparation writes and only reconciles its existing units and readiness.

The manifest freezes command arguments and service environment. Service specs
declare external `config_inputs`; the manifest seals their paths and contents.
Root checks these seals before every native birth, including recovery. Changed
or missing inputs refuse that birth before acquiring process custody. Collector
YAML and the complete native LGTM configuration directory use this contract;
adding a rule file is a configuration change, while backend data writes are not.
This is validation of mutable paths, not atomic exclusion of concurrent writers.
Release publication must provide that exclusion or immutable configuration copies.
The private root
`launch_digest` binds the exact environment dictionary passed to root, the
home's authoritative `.env` and plugin configuration files, and the development
source snapshot (tracked plus nonignored untracked files) at start admission;
root exposes that digest with its native birth in status, without exposing
secrets. The deployment glue selects services and supplies health and diagnostic
participants: [[services/ava_root_glue/ava_root_glue.ava.okf.md]]. The generic
root does not know rollout publication authority or cluster installation.
Development source remains mutable after admission; this check does not certify
an immutable running release or seal ignored dependency directories.

## Native ancestry and lifetime

- macOS: `launchd -> signed permissions helper -> ava-root -> application
  services`. The helper is the permission ancestor. Root start requires the
  durable root-stop and helper-shutdown protocols and proves this exact root birth
  and home. No direct-spawn fallback exists. Helper stop intent survives helper
  restart; an explicit seed resumes the root.
- Linux: `systemd/direct caller -> ava-root -> application services`. There is
  no permissions helper, and “root” does not mean UID 0. A systemd unit invokes
  ordinary start with `Type=notify`. Only its successful readiness tail sends
  acknowledged `READY=1` and `MAINPID`, after checking root birth and matching
  native cgroup; it rechecks manager adoption before returning. No resident boot
  wrapper or root runtime deadline exists. Interactive start has no notify tail.
- Windows: `caller -> ava-root -> application Jobs`. A current-user-only local
  named pipe carries control, an exclusive native file handle owns the singleton,
  and custody publication uses flushed bytes plus write-through rename. Every
  application is created atomically in a retained, non-breakaway Job. Normal
  stop sends Ctrl-Break only to consoles whose complete membership matches the
  captured Job births, then observes zero native members; force terminates that
  original Job. Root death closes the handles and kills members but leaves
  unresolved custody. The desktop helper is separate and is not an ancestor.
  The implementation and native Windows CI fixtures exist; ordinary Windows
  startup remains gated until the native suite actually passes.

Root itself is outside application Jobs so explicit durable terminals can be
born through a deployment resource handler. Each terminal has its own independent
Job owner and native birth record; it survives application-root stop, while full
stop requires its durable empty-Job receipt. Ordinary agent execution stays inside
its caller's Job and cannot request breakaway implicitly. This uses one owner per
terminal rather than another permanent per-home broker.

## Closure and uncertainty

Root records custody before spawning and preserves captured native births before
signals. Normal stop is bounded TERM and exact observed closure; only explicit
force permits KILL. Failed closure retains custody and blocks a replacement.
Root also keeps its control transport alive after failed ordinary shutdown;
new service or resource birth remains closed. An operator can inspect the same
owner, explicitly close its captured domains, then request shutdown again.
Native birth checks reject PID reuse. Missing IPC is unknown, never proof of
absence or readiness. Terminal and execution resources keep their own existing
ownership contracts and are not renamed application service sessions.

Linux `KillMode=process` deliberately signals root alone so independently owned
Postgres, Redis, and PgBouncer siblings can survive application-root shutdown.
Root must close its own application tree. An abrupt root death can leave children;
retained custody blocks cold duplicate launch, but automatic orphan recovery and
proof of independently detached execution-domain closure remain unimplemented.
The native Linux CI test exercises actual manager adoption, root TERM closure,
and retention of a data-process stand-in. It does not prove database durability
or Windows containment. Mac permission grants require real signed-helper proof.

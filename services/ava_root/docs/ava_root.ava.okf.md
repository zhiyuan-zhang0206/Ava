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
`launch_digest` binds root's declared launch environment (`launch_input_keys`), the
home's authoritative `.env` and plugin configuration files, and the development
source snapshot (tracked plus nonignored untracked files) at start admission;
root exposes that digest with its native birth in status, without exposing
secrets. Ambient keys root still hands every unit (proxy, DISPLAY, HOME,
USER/LOGNAME, TMPDIR and the other temp dirs) are outside
the digest: a start that differs only in them reuses the running generation,
which keeps the values it started with, instead of refusing. The deployment
glue selects services and supplies health and diagnostic participants: [[services/ava_root_glue/docs/ava_root_glue.ava.okf.md]]. The generic
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
  no permissions helper, and “root” does not mean UID 0. Automatic boot uses
  only systemd; ordinary convergence registers/enables the exact home unit with
  its checkout and registry. Registration never recursively starts it. The unit invokes
  ordinary start with `Type=forking`. Its final successful dispatch tail publishes
  the exact root PID after checking native birth, home and cgroup. Systemd reads
  that private PIDFile only after the starter exits successfully, when root has
  become its child; native stop therefore waits for root closure. The retained
  child handle prevents early reaping and PID reuse before adoption. PIDFile is
  an adoption hint, never replacement custody. Failed start cannot become ready.
  No resident boot wrapper or root runtime deadline exists; interactive start
  writes no PIDFile. `KillMode=process` preserves independent data-plane siblings.
## Unit intent and recorded failures

Each unit carries one policy fact: its `intent` (`running` | `stopped`) and the
source that last set it (`operator` | `self` | `selection`), persisted per unit
under the run directory (`intent/<unit>.json`, atomic writes). Classification of
an expected stop reads only this intent — a mechanical transition residue never
reads back as an operator action (task #4872). An interrupted replacement is
recorded explicitly as `restart_failed` (the half it failed in, `down` or `up`,
and since when) and clears only once a fresh active generation proves it gone;
the health monitor counts it and retries under its backoff. A root restart
merges each stored record conservatively: an explicit operator or selection stop
holds its unit down, a stop root recorded for itself during shutdown is
superseded by the admitted start, and a recorded failure is carried until a
generation proves it gone.

## Control contract

This package owns the application service tree. The wire protocol and client sit below every consumer:
[[base/native_process/root_control/docs/root_control.ava.okf.md]].

## Closure and uncertainty

Stop certification, retained custody, and release of a leader that exited
before any stop: [[services/ava_root/docs/closure.ava.okf.md]].


## Exact serving generation

Deployment wiring captures loaded source/image identity and canonical home
before root starts application children. Root status binds those facts to its
launch digest and native PID/birth. The ordinary start's serving receipt retains
that generation; readers authenticate the POSIX socket peer and compare the
live native generation before permitting recovery. Replacing root, changing its
launch/runtime identity, or losing observable custody invalidates the receipt.
Generic supervisor fixtures without deployment wiring have no runtime identity
and cannot publish a production serving receipt. This local evidence adds no
fleet authority.

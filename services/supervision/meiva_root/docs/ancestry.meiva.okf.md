---
type: doc
title: Meiva Root — native ancestry and lifetime
description: Where each platform anchors the service tree, and how a root whose permissions helper is replaced retires itself.
tags: [services, lifecycle]
---

# Native ancestry and lifetime

- macOS: `launchd -> signed permissions helper -> meiva-root -> application
  services`. The helper is the permission ancestor. Root start requires the
  durable root-stop and helper-shutdown protocols and proves this exact root birth
  and home. No direct-spawn fallback exists. Helper stop intent survives helper
  restart; an explicit seed resumes the root.
- The root watches that ancestry while it serves. Replacing the helper (a
  `launchctl kickstart -k` does) leaves the root the previous helper spawned
  running unowned: the new helper never adopts a foreign root and will not seed
  another while it holds the instance lock, while the agent hosts below it leave
  at once and stay down (2026-10-17: six minutes, no host for 26 agents). Two
  consecutive broken readings of the marker the helper stamps into every process
  it spawns retire the root instead: it reports `helper_chain_broken` with
  `process=root`, stops its tree, and releases the lock so the keeper seeds a
  replacement from the same seed. The agent host reports the same event with
  `process=agent_host` and exits `HELPER_CHAIN_BROKEN_EXIT_CODE`; a process the
  helper did not spawn carries no marker and never retires. Detection timing is
  `--helper-chain-interval` and `--helper-chain-confirm-rounds` (5 s and 2 by
  default). The macOS helper smoke seeds its root with a 3600 s interval,
  because its conflict phase deliberately keeps an orphan alive across a helper
  replacement to sample attribution in that window (task #3380); disabling the
  watch there is what keeps that measurement meaningful.
- Linux: `systemd/direct caller -> meiva-root -> application services`. There is
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

Parent: [[services/supervision/meiva_root/docs/meiva_root.meiva.okf.md]].

Retiring on a replaced helper is one instance of the rule every generated tree
follows: a component is never retried inside its own container generation, and a
replacement generation is one new chance for the supervisor to rebuild it. The
schedule manager applies the same rule when its session host is replaced — see
[[base/sessions/docs/host-generation.meiva.okf.md|session host generation]] and
[[services/wake/schedule_manager/docs/host-replacement.meiva.okf.md|host replacement]].

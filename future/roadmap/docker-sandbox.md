# Docker isolation — a disposable containerized cluster

Today `execute_code` lands directly in the agent process and `ava.shell`
subprocesses spawn straight onto the bare host — no isolation. The isolation
this needs is **not** a per-agent micro-sandbox; it is a **whole disposable Ava
cluster running inside one Docker** — a complete gateway + Redis + agent-runner +
a set of agents, throwaway.

## Why this framing, and why it is basically there

Run an eval task or a self_evolution run inside a containerized cluster, grade
it, throw the container away. A destructive task ("buy this", "rm -rf") or a bad
self-code-change cannot escape to the host or to the real prod cluster — the
blast radius is the container. That is exactly the boundary the deferred work
needs, and it is the *natural* unit because an Ava deployment is a cluster, not a
lone process.

The important update vs. the earlier "#1 unbuilt gap" framing: **this is mostly
already built.** The multi-host test rig (`../infra/multihost-test-rig-followups.md`,
landed #769/#771) already brings up a full agent-runner + agent spawn inside a
container, validated live. So the substrate exists; this is a reuse, not a
from-scratch sandbox.

## What's actually left

1. **Wire the throwaway cluster as the eval / self_evolution substrate** — spin
   up a clean containerized cluster, run the task/run inside it, tear it down.
   The bring-up exists (test rig); making it a first-class eval/self_evolution
   primitive is the work.
2. **The promote-on-pass path** — a self-code-change (or any candidate) that
   passes its eval *inside the container* is what gets promoted to the real
   cluster via the normal PR -> CI -> merge -> `ava cluster update` loop. The
   container is where it is *tried*; the existing rollout is how it *ships*.

## Scope nuance (kept honest)

Cluster-in-a-container protects the **host** and the **prod cluster** — that is
what eval, self_evolution, and destructive-task isolation actually require. It
does **not** isolate one agent from another *within* the same container; that
finer boundary is not needed for these use cases and is not in scope.

## What it unblocks (the fuse)

1. **Permissions / approval model** — gains real teeth once code is confined to
   the container; ship them with this. Likely shape: container-level capability
   boundaries, not per-tool allow/deny prompts.
2. **Autonomous self-code-evolution** ([`self-code-evolution.md`](self-code-evolution.md))
   — the disposable cluster is the safe place to let it rewrite + test itself
   before anything is promoted.
3. **Arbitrary write-on-peer-machine** — the documented non-goal "until a
   sandbox boundary lands"; the container is that boundary.

## Reconcile the charter note

[`non-goals.md`](../../conventions/non-goals.md) lists sandbox as a V1 non-goal ("runs bare on the
host") while the small-core charter lists sandbox as explicitly **not**
strippable ("safety is never free"). This item resolves that: the boundary is
the disposable containerized cluster, and it is close, not far — most of it
already runs in the test rig.

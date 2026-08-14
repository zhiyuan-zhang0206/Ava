# Multi-Machine

Multi-machine is the default shape, not a premium configuration. A cluster is
one gateway machine (owns the data plane) plus any number of agent-runner
machines (only execute agents). A single box is just the N=1 case — there is
no flag and no opt-in.

## Why it matters

- **Scale out by adding machines** — each runner adds agent capacity; the
  gateway owns Postgres/Redis and the one HTTP control surface.
- **Windows joins as a runner** — natively, no WSL, no Docker; macOS, Linux
  and Windows are all supported.
- **Secure by default** — cluster authentication is always on and fail-closed;
  the data plane binds loopback plus the host's own address only.

## How it works

Any machines that are **network-reachable to each other** form a cluster: run
`install.sh --role gateway,agent-runner` on the gateway box, and
`ava enroll --gateway <url>` on each runner. The gateway orchestrates rollouts
across the whole roster; a pure runner self-updates on the pinned commit.

<!-- TODO(image): cluster topology — gateway + N runner machines -->

## Real usage

- [`traces/cluster-update.md`](../../traces/cluster-update.md) — a rollout
  trace observed across a production cluster spanning four machines.

## Design decisions

- [Multi-host deployment: single-box is the N=1 case](../../decisions/2026-06-11-multihost-deployment.md)
- [Windows agent-runner only](../../decisions/2026-07-28-windows-agent-runner-only.md)
- [Windows setup](../../conventions/windows-setup.md)

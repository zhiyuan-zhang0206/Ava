# Multi-Machine

Multi-machine is the default shape, not a premium configuration. A cluster is
one gateway machine (owns the data plane) plus any number of agent-runner
machines (only execute agents). A single box is just the N=1 case — there is
no flag and no opt-in.

## Why it matters

- **Scale out by adding machines** — each runner adds agent capacity; the
  gateway owns Postgres/Redis and the one HTTP control surface.
- **POSIX hosts join as runners** — macOS and Linux, including Linux inside WSL2.
- **Secure by default** — cluster authentication is always on and fail-closed;
  the data plane binds loopback plus the host's own address only.

## How it works

Any machines that are **network-reachable to each other** form a cluster: run
`ava init --serve-gateway --serve-agent-runner --machine-name <name>` and `ava start` on the
gateway box, and, with the capability bundle the gateway issues
(`ava cluster db-authority issue-unit`) and its transport key exported,
`ava init --no-serve-gateway --serve-agent-runner --gateway-url <url> --machine-name <name> --machine-host <host> --db-capability <bundle>` and `ava start` on
each runner (a runner never holds the gateway's cluster secret). `ava cluster status` shows every host's running commit; release
transitions currently act on one home at a time, and fleet-wide transitions are
planned work ([unified cluster lifecycle](../../future/infra/unified-cluster-lifecycle.md)).

<!-- TODO(image): cluster topology — gateway + N runner machines -->

## Design decisions

- [Multi-host deployment: single-box is the N=1 case](../../decisions/2026-06-11-multihost-deployment.md)
- [Windows host setup](../../conventions/windows-setup.md)

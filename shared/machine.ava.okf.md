---
type: doc
title: Machine Identity & Capabilities
description: '`shared/machine.py` — this host''s stable name plus its capability SET (`gateway` and/or `agent-runner`). A host runs the union of its capabilities'' services; the `machines` table is the cluster-wide view of the same two labels.'
tags:
- shared
- library
- cluster
---

# Machine Identity & Capabilities

## What it is

Two labels define a host in a multi-machine deployment, both resolved by
`shared/machine.py`:

- **`machine_name()`** — the stable identifier. The memory git branch
  (`machine-<name>`), `agents_meta.machine`, and the `machines` table PK all read
  from it. Deliberately **not** `socket.gethostname()`, which drifts on macOS
  when switching wifi.
- **`machine_role()`** — a **frozenset** of capabilities, not a single role.
  Prefer `is_gateway()` / `is_agent_runner()` over comparing the set.

Precedence for both: env var > `$AVA_HOME/<field>` file > fail loud
(`MachineNameMissing` / `MachineRoleMissing`). The role comes from two
*independent* booleans — `AVA_MACHINE_SERVE_GATEWAY` and
`AVA_MACHINE_SERVE_AGENT_RUNNER` — so a single box is not a third role, just
both flags true.

There is **no TTY prompt**: `ava start` writes the files from its flags, and a
missing value prints an actionable error and exits 1. An agent calling `ava`
(e.g. `ava cluster status`) has no TTY and would hang on a prompt.

## The two capabilities

| Capability | Owns | Data plane it uses |
|---|---|---|
| `gateway` | the HTTP gateway + this cluster's Postgres / Redis / Milvus + the gateway-side daemons | its own local instances |
| `agent-runner` | the agent host + the ops server | its own local instances when the host is also `gateway`; otherwise a gateway node's, via `AVA_DB_URL` / `AVA_REDIS_URL` / `AVA_MILVUS_URI` in its `.env` |

A host runs the **union** of its capabilities' services — the per-service
capability declaration is `ServiceSpec.capabilities` in `ops/spec.py`, and the
resulting roster is documented in [[../services/services.ava.okf.md]]. A **single box**
(`gateway,agent-runner`) is therefore not a special case in the code, just the
host where both sets are non-empty and the reachable address is loopback.

**Only one gateway per cluster.** The labeler / heartbeat / report /
memory-indexer daemons would race on the same DB rows if two ran, and one
frontend is sufficient.

## The `machines` table

The cluster-wide roster. `gateway_url` holds whatever address the cluster dials
this host at, resolved once by `shared.machines.unit_dial_url(roles)`: an
**agent-runner-capable row carries its ops server URL** (`http://localhost:<ops_port>`
co-located, `http://<reachable-host>:<ops_port>` split) — that is the dial target
spawn/lifecycle forwarding POSTs to; a gateway-only row carries its own gateway URL,
informational.

Two processes write that row, both through `register_self()`: `ava start` at the
tail of a supervised bring-up, and the unit's **ops daemon at its own boot**. The
second is what makes `stopped_at` and `up_since_at` mean what their readers assume
— a host brought back by an autostart or a watchdog respawn un-stops itself instead
of serving ops under a stale `stopped` marker. `up_since_at` is a boot/announce
stamp, not a heartbeat — the name it earned in #981, after `last_seen_at` had spent
its life promising a heartbeat these tables have never had; liveness is the live
`status_probe`
([why](../decisions/2026-07-29-liveness-is-written-by-the-live-process.md)).

Cross-machine spawn is a direct dial, not a queue: the gateway POSTs a `spawn`
op to the target runner's ops server (address read from this table), which calls
`ops/ops_lifecycle.py:launch_agent_op` in-process and returns the result in
the same response. Runners run **no** local gateway process — an agent's SDK
reaches the gateway over HTTP via `gateway_api_base()`.

**Spawn-target invariant**: `POST /api/agents` returns **HTTP 400** when the
resolved `machine` lacks the `agent-runner` capability (`'agent-runner' =
ANY(role)` is false) — typically a pure gateway node. A single box satisfies it,
so a local spawn there is allowed. The guard exists to catch "the UI booted an
agent onto a gateway-only node where only the scheduling/DB layer lives".

## Notes

- Any endpoint returning or fanning out per-machine data must declare its
  role scope; the classification rule is in
  `conventions/python-conventions.md`.
- Enrolling a split runner (`ava enroll`) does not birth a cluster — its cluster
  identity **is** the gateway URL + cluster secret it presented.

## Key Dependencies

- [[../services/services.ava.okf.md]] — which services each capability contributes
- [[paths.ava.okf.md]] — where the `machine_*` files live
- [[../cli/cli.ava.okf.md]] — `ava start` / `ava enroll`, which write these labels

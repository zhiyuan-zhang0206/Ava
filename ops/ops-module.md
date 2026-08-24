# Ops module — final state

Blueprint for the unified ops layer. The design decision is
[`2026-07-19-ops-k8s-semantics-without-k8s.md`](../decisions/2026-07-19-ops-k8s-semantics-without-k8s.md);
this describes the end state, and sequencing is left to the implementing agents.

> **Status: mostly built. Two rows of the vocabulary table below are still
> unrealized** — `ops/identity.py` and the shared Drain primitive.
> Landed: `ops/spec.py` (the desired-state expression), `ops/observe.py`, the
> controller set under `ops/controllers/` (respawn, pin, schema, stranded-pause,
> hibernate, resurrect, wedged, update-trigger, over a shared `base`), and
> `ops/manager.py` as the tick loop. `services/watchdog/daemon.py` shrank from the
> ~680-line mix to a thin ~320-line main over that controller list. The
> import-linter contract `shared < ops < {gateway, cli}` is enforced in
> `pyproject.toml`.

## Vocabulary (K8s-shaped)

| Concept | Ava realization | Replaces (scattered today) | State |
|---|---|---|---|
| Spec | `ops/spec.py` — the single expression of desired state | `build_services()` + registry + `cluster_pin` + `.env` identity + prose | ✅ built |
| Status | `ops/observe.py` — probe set | `services/healthchecks/` + status commands | ✅ built |
| Controller | `ops/controllers/<dimension>.py`, one per state dimension | watchdog's inline gates + restarter reapers | ✅ built |
| controller-manager | `ops/manager.py` — tick loop running the controller list | `services/watchdog/daemon.py` (680-line mix) | ✅ built |
| CronJob | scheduled jobs: pg-backup, debt sweeper | pg-backup hand-appended to the watchdog tick (`_checks_for_capability`) | ✅ built — pg-backup is a supervised scheduler `ServiceSpec`; debt sweeper remains separate |
| Drain | agent quiesce as a shared primitive | update and stop each rolling their own | ⬜ not built — `_quiesce_all_agents` still lives in `cli/commands/update.py` |

## Spec content

What this machine-role should run (service roster — absorbed from
`build_services()`; watchdog/restarter/healthchecks all derive from it — the
watchdog keepalive roster already does, via `ServiceSpec.healthcheck_module` (I-5);
restarter/healthchecks fold in next), what data plane
should exist (instances, roles, redis ACL users, bind addresses), what SHA the
cluster is pinned to, which deployment-identity values hold (home path, ports,
machine host, secrets — migrated from `Settings`, see
[`2026-07-19-config-ownership-decomposition.md`](../decisions/2026-07-19-config-ownership-decomposition.md)).

## Controllers (initial inventory)

Built: process-respawn (from restarter, incl. gateway health gating + orphan
reapers), schema (migrations behind → trigger update), pin (checkout drift),
stranded-pause, plus hibernate / resurrect / wedged, which the inventory did not
anticipate. Not built: redis-acl and data-plane-config (bind addresses / auth as
declared) — both still reconcile outside the controller set. Each controller:
observe → diff against spec → act, with its own cooldown/backoff; every heal logs
and surfaces in status; repeated healing of one dimension escalates to an alert. A
controller that blocks a round states only how wide its finding is
(`BlockScope`: whole host vs the DB's users) and never names services — the
scope-to-service match is the watchdog's, against each `ServiceSpec.requires_db`.

## Probe contract principle

A probe verifies the contract dependents actually rely on, not liveness:
connect to pg as the real role with the real secret (the false-green class),
probe the frontend (it went down unannounced once), HTTP `/healthz` for
daemons. The probe audit is part of reaching this state.

## Declarative rollout — PARTIAL

The pin controller reconciles an off-pin agent-runner toward the target SHA, so the
runner half is declarative. `ava update` itself is still the imperative
orchestration (drain → checkout → sync → migrate → restart, fanned out from the
gateway), and the drain step inside it is the un-extracted Drain primitive above.

`ava update` = write the target SHA into spec; per-machine controllers
converge (drain agents → checkout → sync → migrate → restart); rollout
watches status until all machines report the target. No imperative
orchestration path remains — pin was already reconciler-driven, and the
imperative/declarative mix is what made watchdog fight manual fixes.

## Cluster identity — NOT BUILT

`ops/identity.py` does not exist; `resolve_ava_home` still lives outside the ops
layer and the AGENTS.md prose wall is still prose. The target is the same —
resolution as one pure function with an exhaustive truth-table test, and the prose
wall shrinking to a pointer at it — but the *content* has moved on: identity is now
path-only (a cluster's identity IS its home path, born at install time, with no
cluster name and no `--cluster` flag to pass), so the truth table is over
`AVA_HOME` env / prod source / the checkout's `.ava_home` pointer, not over worktree
roots and name flags. See
[`2026-07-20-path-only-cluster-identity.md`](../decisions/2026-07-20-path-only-cluster-identity.md).

## State-dimension inventory

The admission gate for self-heal (see
[`2026-07-19-fail-fast-vs-reconcile-boundary.md`](../decisions/2026-07-19-fail-fast-vs-reconcile-boundary.md)):
a table of dimension × reconciler × verification. Seed: redis ACL (reconciled, but
as a watchdog healthcheck, not a controller), pg password (none today), pg bind
(none), `.env` vs process env (none), frontend health (none), schema version (✓),
pin (✓), stranded pause (✓). "No spec entry, no self-heal."

## Process provider

The Spec execution layer — how a controller actually starts / stops a service — is
a multi-backend **process provider** abstraction: a reconciler decides *what* should
run (a `ServiceSpec`), a provider decides *how* to run it on this host's OS.
Backends: the native process supervisor for services (POSIX
`shared/posixproc.py` via `PosixProcSessionBackend` — the 2026-08 session-migration
step 1; winproc on Windows), per-session pty hosts for agent interactive shells /
watchers (`shared/pty_sessions/` via `PtySessionBackend` —
`get_shell_backend()`; each session in its own detached host, no supervisor
service), the session backend for the orchestration sessions too
(S7 — updater / rollout / cluster-restart, `get_backend()`; the old
`get_orchestration_backend()` is deleted), and — deferred — launchd (macOS),
systemd (Linux). The abstraction is a seam, not extra machinery: step 1 moved
daemon/service sessions onto the native supervisor; step 2 moved agent shells
onto the PTY supervisor; what remains deferred is the launchd/systemd drivers.
Every service routes through the session backend — `shared.service_respawn.respawn_service`
(the healthcheck respawn) and `cli.commands._session_lifecycle._new_session` (`ava start`);
that *is* the current provider.

**Fork / zygote is rejected** as the execution model. A resident pre-warmed template
process that `fork()`s per spawn (to skip the agent import tax) breaks the "one
session per agent" model that the kill paths / visibility / per-cluster session
records all lean on, and the flat spawn cost curve does not justify it — the horizontal
answer (agents brought up asynchronously and independently, scaling memory out
rather than sharing a warm parent) is what the deployment already does. Full
rationale: [`non-goals.md`](../conventions/non-goals.md) (fork-from-warm / zygote).

## Boundaries

- `shared < ops < {gateway, cli}`, inside the import-linter contract.
  `services/` daemons become thin mains over ops controllers.
- Wire schemas split by **consumer layer** (import-linter): the gateway-only HTTP
  schemas live in `gateway/schemas/` (by router family); the RPC-shared types the
  agent-runner (`ops/` handlers + `services/` daemons) also consumes stay one layer
  down in `ops/rpc_schemas.py`, because ops must never import up into gateway —
  gateway imports them downward instead; and the HTTP API contracts the `cli` thin
  clients also decode (`MachineStatus` / `Config*`) are downshifted a further layer
  to `shared.api_contracts`, re-exported by `gateway.schemas` under their unchanged
  OpenAPI names, so `cli` validates a gateway response without importing up. See
  [`2026-07-19-config-ownership-decomposition.md`](../decisions/2026-07-19-config-ownership-decomposition.md).
- Non-goals: no K8s/k3s runtime, no image-based deployment (re-evaluate only
  for a headless Linux fleet shape).
- CLI verbs thin down to spec edits + convergence watching (`_cluster_instance`,
  `_converge`, `update`, `cluster_lifecycle` mass migrates here).

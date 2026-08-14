# Multi-machine deployment: single-box is the N=1 case, auth is the foundation

> **Decision date 2026-06-11** (governing principle, §2); the auth-as-foundation
> restart that re-sequenced §10 into rungs was decided 2026-06-14. **Shipped
> 2026-06-15** — every §10 rung landed and the `AVA_MULTIHOST_ENABLED` flag was
> deleted outright (`c55d8136`); there is no flag and no opt-in.
>
> ⚠️ **READ AS HISTORY, NOT AS CURRENT FACT.** This entry is a point-in-time
> record kept for its *why*: the "single-machine is a degenerate multi-machine
> cluster" principle (§2), the topology it produced (§3), and the ladder that
> sequenced the work (§10). **§4, §5 and §9 describe the pre-restart posture that
> this decision replaced** — an unauthenticated gateway, Postgres `trust` with no
> password, redis with no password, and an opt-in flag. **None of that is true
> today.** Each of those sections carries its own banner; do not read a paragraph
> out of them without it.
>
> Current behavior: auth is always on and fail-closed (the cluster secret is
> required on every host, the gateway refuses to start without one;
> `/api/bootstrap` + `/ops` always authenticate; pg `scram-sha-256` + redis
> `requirepass`), pg/redis bind loopback + `AVA_MACHINE_HOST` only, trust ranges
> are config (`AVA_TRUSTED_CIDRS`), and the agent-facing machine surface is always
> present (a single box simply sees one machine). Read it from
> [`runbook.md`](../conventions/runbook.md) /
> [`dev-setup.md`](../conventions/dev-setup.md), the OKF graph
> ([`okf/index.ava.okf.md`](../okf/index.ava.okf.md)), and the per-field
> `scope` metadata in `shared/config/`.

---

## 1. What this is and is not

**Goal.** Run one logical Ava cluster across several physical machines on a
Tailscale tailnet: a small always-on **gateway** owning the data plane, plus one
or more **agent-runner** hosts (a beefier box, a corp laptop, a WSL machine)
that only execute agents. The single-box shape (one host carrying
`gateway,agent-runner`) stays the default; this is the opt-in scale-out.

**Non-goals** (see [`non-goals.md`](../conventions/non-goals.md)): a public
internet edge, multi-tenant isolation, HA / replicated Postgres, or a message
queue between gateway and runners. The tailnet is assumed to be a single trust
group of machines the one operator owns. Cross-WAN federation of independent
clusters is out of scope.

**Two independent axes, not one flag.** Auth is always on (`AVA_CLUSTER_SECRET`
required everywhere; `/api/bootstrap` + `/ops` always authenticated; pg role
password + redis `requirepass` = the secret), and the data-plane bind follows
whether an overlay address is declared (`AVA_MACHINE_HOST`: undeclared = loopback
only, declared = loopback + the overlay address + the `AVA_TRUSTED_CIDRS` pg_hba
ranges). Splitting those two axes apart is what Rung 1 + Rung 2 of §10 delivered;
the "one flag gates bind/auth/bootstrap" framing in §4/§5/§9 below predates it.
The residual `AVA_MULTIHOST_ENABLED` flag — which at the time of writing still
gated the agent-facing machine surface (`spawn(machine=)`, `list_machines`, the
roster in the system prompt) — was deleted on 2026-06-15; that surface is now
unconditional.

---

## 2. Core design principle: single-machine is a special case of multi-machine

> **Governing principle (CTO, 2026-06-11).** The single-box deployment is not a
> separate mode with local shortcuts — it is a *degenerate multi-machine cluster*
> whose one host happens to carry both capabilities. Every code path is written
> for the multi-machine case; single-box falls out as the N=1 instance.

Four rules:

1. **The gateway never reaches across to read an agent-runner's data directly.**
   It always goes through the standard path — dial the runner's ops server, or
   let the runner's own process own its local state. No "it's the same box, just
   read the file / hit the DB directly".
2. **An agent-runner only manages its own machine.** It never reaches into
   another host or into the gateway's internals; it answers ops dialed *to* it.
3. **All gateway ↔ agent-runner communication goes through the standard path** —
   the SDK→gateway HTTP surface (`gateway_api_base()`) and the gateway→ops dial
   (`cluster_rpc.dispatch_to_machine`). No side channels.
4. **No `is_single_machine` / `is_gateway()` branch that takes a local shortcut.**
   The same code runs whether the gateway is across the tailnet or on the same
   box; only the *resolved address* differs, and it differs by **config**, not by
   a code branch.

**Why this matters.** Every `if same-machine: shortcut` is a second code path
exercised only in the single-box default, silently rotting in the split case (or
vice versa). Collapsing to one path means a split deployment is exercised by
every single-box run and the reverse — the N=1 case can never drift from N>1.
This is the deployment-layer expression of the codebase's "small core, one path"
stance.

**How rule 4 is satisfied — address by config, no code branch.**
Uniformity is in the **code path**; the address is a **config value**.
`gateway_api_base()` resolves the *one* configured `AVA_GATEWAY_URL` (env > file)
for every caller, role-blind, with no `is_gateway()` branch. What differs is the
configured value — by where the gateway sits *from that unit's view* — and that
lives entirely in config: a gateway box's `.env` holds `http://localhost:<port>`
(a box reaches its own gateway over loopback — `derive_env` materializes it at
birth), a remote agent-runner's `.env` holds the gateway's reachable URL, handed
to it at `ava enroll --gateway`. A box never needs to know its *own* reachable
address to call itself; that address exists only in each remote runner's enroll
config. One path, address by config — that *is* "single-machine is a special case."

> **Why loopback (not the box's own reachable address) for self-calls.** macOS
> does not route a host's connection to its *own* Tailscale IP back to a local
> listener (no hairpin), so a gateway box configured to dial itself over its
> tailnet address times out — even though remote peers reach that same address
> fine. Loopback is the correct, dependency-free same-machine path. This
> **supersedes** an earlier stance (single-box self-calls should traverse the
> reachable address for path-uniformity, accepting a tailnet-is-always-up
> trade-off): the reachable address is now a remote-only concern, set at enroll,
> never the gateway's own self-URL.

`gateway_api_base()` is already role-blind (no localhost branch) and a gateway
box's self-URL is loopback-by-config, so the communication plane is principle-clean
on this point. The one remaining rule-4 delta is `list_agent_runners()`'s
query-scope branch (§10.B).

---

## 3. Topology — the tailnet as one trust group

```
                     Tailscale tailnet (WireGuard mesh, one trust group)
                     CGNAT 100.64.0.0/10  +  fd7a:115c:a1e0::/48
  ┌───────────────────────────────────────────────────────────────────────────┐
  │                                                                             │
  │   ┌──────────────────────────┐         ┌──────────────────────────────┐    │
  │   │ GATEWAY host             │         │ AGENT-RUNNER host(s)         │    │
  │   │ role gateway[,agent-...] │         │ role agent-runner            │    │
  │   │ home ~/.ava              │         │ home ~/.ava                  │    │
  │   │                          │         │                              │    │
  │   │ native Postgres :5433  ◄─┼─────────┼── AVA_DB_URL (data plane)    │    │
  │   │ native Redis   :6380  ◄──┼─────────┼── AVA_REDIS_URL              │    │
  │   │ Milvus :19530 (local)    │         │                              │    │
  │   │ gateway FastAPI :8000  ◄─┼─────────┼── SDK -> gateway_api_base()  │    │
  │   │ frontend Next :3000      │         │                              │    │
  │   │ labeler/indexer/telegram │         │ ops server :<ops_port> ◄─────┼─┐  │
  │   │ ops/restarter/watchdog   │         │ restarter / watchdog         │ │  │
  │   │                          │  /ops   │ agent-{N} processes          │ │  │
  │   │ cluster orchestrator ────┼────────►│ (POST /ops, in-process)      │ │  │
  │   └──────────────────────────┘         └──────────────────────────────┘ │  │
  │            ▲                                  the gateway DIALS INTO ─────┘  │
  │            │ browser :3000 -> gateway :8000                                 │
  │   ┌────────┴─────────┐                                                      │
  │   │ User devices     │  laptop / phone — also tailnet peers                 │
  │   └──────────────────┘                                                      │
  └───────────────────────────────────────────────────────────────────────────┘
```

Two machine **capabilities** (a host carries a frozenset of one or both —
`shared/machine.py`):

- **`gateway`** — owns Postgres / Redis / Milvus + the FastAPI gateway + the
  coordination daemons (labeler, memory-indexer, telegram, frontend) + the
  cluster orchestrator. **Exactly one host per cluster** carries it; the
  coordination daemons would race on the same DB rows if duplicated.
- **`agent-runner`** — runs agent processes + the ops server + restarter +
  watchdog. Many per cluster. Holds no data plane; its `AVA_DB_URL` /
  `AVA_REDIS_URL` point at the gateway.

**Single-box anatomy under the §2 principle.** A single-box host is the union
(`gateway,agent-runner`, one `~/.ava`) — but it is *modeled as a one-node
cluster*, not as a special mode. Its agent-runner half reaches its gateway half
through the same `gateway_api_base()` path any remote runner uses — role-blind,
**by config not a code branch**: the box's own `.env` configures the gateway URL to
loopback (a box reaches its own gateway over loopback), while a remote runner's
`.env` configures the gateway's reachable address. One resolver, the value supplied
by config. Likewise the box's data-plane and ops dials are the same calls a split
deployment makes — the diagram above collapses to one host, but no edge in it is
replaced by an in-process shortcut. A **split** deployment is the same picture with
the two boxes physically separated.

**Cluster identity** (name → db name, its own per-cluster Postgres + Redis
instance and ports, port block) is born at `ava start` and inherited by
runners through bootstrap; mechanics in `shared/cluster.py` +
[`runbook.md`](../conventions/runbook.md).

*(Cluster identity has since moved to install time and is path-keyed, with no
cluster name — see [`2026-07-20-path-only-cluster-identity.md`](2026-07-20-path-only-cluster-identity.md).)*

---

## 4. Communication — who dials whom, over what

> ⚠️ **Pre-restart snapshot.** The table below marks the control plane "unauth"
> and the data plane `trust` / no-password. **That is the posture this decision
> replaced**, not current behavior: every plane authenticates today (Bearer
> cluster secret or session cookie on `/api/*`, pg `scram-sha-256`, redis
> `requirepass`), fail-closed. The *shape* — who dials whom, one synchronous
> round-trip, no queue — is what this section is kept for.

The design rule (a direct corollary of §2): **the gateway is the only inbound
HTTP surface a client targets; the gateway is the only dialer of runner ops
servers.** Two planes ride the tailnet, both plaintext over WireGuard (the tunnel
is the encrypted transport — no app-layer TLS, see §5):

| Plane | From → To | Port (main cluster) | Protocol | Direction |
|---|---|---|---|---|
| **Control: SDK → gateway** | agent-runner agent / frontend → gateway | `:8000` | HTTP, unauth | runner-initiated |
| **Control: gateway → runner** | gateway orchestrator → runner ops | `:8106` (`<ops_port>`) | HTTP `POST /ops`, unauth | gateway-initiated |
| **Data: runner → gateway PG** | agent-runner processes → Postgres | `:5433` | psql wire — today `trust` (no password); `scram-sha-256` from Rung 2 (§10) | runner-initiated |
| **Data: runner → gateway Redis** | agent-runner processes → Redis | `:6380` | RESP — today no auth (`protected-mode no`); `requirepass` from Rung 2 | runner-initiated |
| **Page: browser → agent page** | user browser → `ava.ui` page server | ephemeral | HTTP | browser-initiated |
| **Browser → gateway** | user browser → frontend `:3000` → gateway `:8000` | `:3000`/`:8000` | HTTP | browser-initiated |

Key properties (the design intent behind the wires):

- **No queue, no callback.** A control op is one synchronous round-trip:
  `gateway/cluster_rpc.py:dispatch_to_machine` resolves the runner's ops URL from
  the `machines` table and POSTs `{kind, payload}` to `{ops_url}/ops`; the runner
  runs it **in-process** by calling `gateway/ops_*.py` and answers in the HTTP
  response (`services/agent_ops/daemon.py`). The full op vocabulary
  (`OpKind`): `spawn`, `lifecycle`, `cluster_stop`, `cluster_update`,
  `cluster_resume`, `status_probe`, `config_read`, `config_write`,
  `inventory_read`, `inventory_write`. This is the *standard path* of §2 rules 1
  & 3 — there is no "same box, skip the dial" variant. A blackholed (powered-off)
  peer hangs in connect, so the dispatcher caps connect at `min(10s, timeout)`.
- **The SDK is topology-blind.** Agent code calls `ava.*`, which always goes to
  `shared.machine.gateway_api_base()` (architecture §6.1). Under §2 this resolves
  the one configured `AVA_GATEWAY_URL` for every caller, role-blind (no
  `is_gateway()` branch). A single-box agent-runner reaches its co-located gateway
  over loopback — that is the gateway box's configured self-URL (`derive_env`
  materializes `http://localhost:<port>` into `.env`), the correct same-machine
  path, by config not a code branch. The gateway *then* decides whether to handle
  locally or re-dial the agent's home runner; agents never learn the cluster shape.
  (Formerly tracked as rule-4 violation §10.A — now resolved; see the self-call
  note in §2.)
- **Reachable address is operator-declared, never auto-detected.**
  `AVA_MACHINE_HOST` (or `$AVA_HOME/machine_host`) is each host's tailnet
  address; `reachable_host()` raises rather than guess. This is the single point
  where the codebase "knows" about the network — deliberately abstract so the
  overlay (Tailscale here) is swappable, and the single hook §2's "address by
  config" relies on. **Prefer MagicDNS names**
  (`<host>.<tailnet>.ts.net`) over raw `100.x` literals: the roster's
  IPs drift on re-registration (the `machines` table PK is `machine_name`, not
  IP), and on some networks (e.g. a phone on a cellular hotspot) a raw `100.x`
  literal is NAT64-synthesized and times out while MagicDNS resolves correctly.

**Reviewing every path against §2.** Control (SDK→gateway, gateway→ops) and data
(runner→PG/Redis) are all already the standard path. `gateway_api_base()` is
role-blind and a gateway box's self-URL is loopback-by-config (a box reaches its
own gateway over loopback — §2), so the communication plane is principle-clean;
the one remaining rule-4 delta is `list_agent_runners()`'s query scope (§10.B).

---

## 5. Security — the load-bearing boundary and the gap to close

> ⚠️ **This whole section describes the security posture as it stood on
> 2026-06-11, which this decision exists to end. It is NOT how Ava is deployed.**
> "The tailnet IS the auth", the unauthenticated `/api/bootstrap`, pg `trust` with
> no password, redis `protected-mode no` on `0.0.0.0` — all of it was closed by
> Rung 1 + Rung 2 and shipped 2026-06-15. §5.3 is the plan that closed it, and it
> is the reason this section is kept.

### 5.1 Starting posture (superseded 2026-06-15): the tailnet IS the auth

The gateway is **unauthenticated**; pg_hba grants the Tailscale CGNAT ranges
(`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) `trust` — **no password at all**, not
even a shared one (`_pg_hba_body()` in `cli/commands/_compose.py`); redis runs
`protected-mode no` bound `0.0.0.0` with no password. `0.0.0.0` exposes both the
overlay interface *and* the host's physical NIC, and redis has no source filter
at all — so a same-LAN neighbour outside the CGNAT range, who pg_hba would
reject, can still reach redis. The managed config blocks are rewritten by
`_compose.py` on each `ava start` keyed on `multihost_enabled`.
**The WireGuard mesh is the entire trust boundary.** Anything that can route a
packet to a port is trusted as a cluster peer. (Rung 1 closes the redis-on-LAN
hole by binding the overlay interface; Rung 2 adds the missing passwords — §10.)

This is acceptable **only** because the tailnet is one operator's private mesh.
It is the explicit blocker for two things: making multi-host the default, and
open-sourcing the repo. Concretely, `GET /api/bootstrap` hands out `db_url` and
every cluster secret **unauthenticated** to anyone who can reach `:8000` — which
is why it 404s unless `multihost_enabled` is on.

### 5.2 Tailscale ACL design (recommended, not yet applied)

Tailscale defaults to allow-all between a tailnet's own devices. The design
should **tighten** to least-privilege via tagged ACLs, so a compromised runner
cannot reach the data plane on ports it has no business on:

- **Tags**: `tag:ava-gateway`, `tag:ava-runner`, `tag:ava-user` (laptops/phones).
- **ACL grants** (intent):
  - `tag:ava-runner` → `tag:ava-gateway` on `5433, 6380, 8000` only.
  - `tag:ava-gateway` → `tag:ava-runner` on `<ops_port>` only.
  - `tag:ava-user` → `tag:ava-gateway` on `3000, 8000`; → any host on the
    ephemeral page-server range for `ava.ui`.
  - No runner → runner path (agents never talk to each other directly — the
    `inbound_messages` table is the only bus; this matches §2 rule 2).
- **Tailscale SSH** + ACL for operator access, replacing scattered `ssh` key
  distribution.
- **MagicDNS on**, so config uses stable names not drifting IPs.

This is a pure tailnet-policy change (one ACL file in the Tailscale admin), zero
code. It does **not** replace app-layer auth — it shrinks the blast radius of the
unauthenticated surfaces below. **NOT yet written/applied** — today the tailnet
is flat allow-all.

### 5.3 The auth foundation: reachability ≠ trust (the restart's core)

This is no longer a "later" item — it is the foundation the restart is built on,
sequenced in two rungs (§10). The work, smallest-first:

- **A single cluster join secret (Rung 2 — the deliverable).** One shared secret
  the operator supplies out-of-band at enroll. The control plane (`/api/bootstrap`,
  `/ops`, spawn) requires it on every call; the data plane derives a pg
  `scram-sha-256` password and a redis `requirepass` from it. This alone moves
  the cluster off "tailnet membership == ownership" — reachability stops being
  trust. Enough for a single operator with a handful of machines.
- **Per-machine keys + Postgres roles (Rung 3 — refinement).** Mint a per-machine
  credential + a per-machine `ava` role at enroll instead of the one shared role
  (scheduled secret rotation was weighed against this and decided against):
  decommissioning a host becomes `DROP ROLE`, a leak revokes one host without
  churning the cluster's shared secret. The capability scheduled rotation is a
  blunt proxy for.
- **Bootstrap secret scoping (Rung 3).** Even with keys, `/api/bootstrap` returns
  *all* cluster-pinned secrets; a finer design hands a runner only what its
  capability needs.

Until Rung 2 lands, the honest security statement holds: **multi-host trusts the
tailnet completely; a single hostile tailnet device owns the cluster.** That is
why it is opt-in — and why Rung 2 is the bar for "safe to opt in".

> Rung 2 landed 2026-06-15, so the statement above expired with it: reachability
> is no longer trust. Rung 3 (per-machine credentials / roles) and the Tailscale
> ACL tightening of §5.2 were **not** taken and remain unbuilt.

---

## 6. Config management across machines

Driven entirely by the **ownership scope** every `Settings` field declares —
full model is the per-field `scope` metadata in `shared/config/`; the
multi-machine-relevant shape:

- **`cluster-pinned` / `cluster-default`** live in the **gateway's `.env`**.
  Runners fetch them at process start via `GET /api/bootstrap`
  (`shared/bootstrap.py:inject_config_from_gateway` — fetched values are
  authoritative, overwriting env/.env, since 2026-08-01; and the session-env
  handoffs stopped forwarding cluster-scope values at all on 2026-08-02, so a
  spawner's frozen copy never reaches a daemon). `BOOTSTRAP_FIELDS` is *derived*
  from scope, so a new cluster field reaches runners automatically. A rotated
  cluster secret reaches an agent on its next restart without the gateway
  restarting (bootstrap reads the gateway `.env` fresh).
- **`host`** fields live in **each machine's own `.env`** — never distributed.
  The gateway can *propose* a remote edit via `PUT /api/config?machine=<name>`
  (dispatched as a `config_write` op), but the **receiving host validates against
  its own reality and disposes** (a headless host rejects `browser_enabled`).
  This is §2 rule 2 in the config plane: the gateway never writes a runner's local
  state directly — it asks, the runner decides. `remote_writable` is a
  default-deny allowlist; identity/connection fields (`machine_name`,
  `machine_serve_gateway`, `machine_serve_agent_runner`, `machine_host`,
  `gateway_url`) are never remotely editable — editing them would brick the host.
- **Plugin / MCP enable** is host-local overlay JSON (`plugins_config.json` /
  `mcp_enabled.json`), read through / written through per host — same
  gateway-proposes/host-disposes model via `inventory_read|write` ops. Never in
  the DB.

The design principle: **cluster identity is centralized and pushed; everything
that describes a specific machine stays on that machine and is at most proposed
to.** This is why a runner needs only a tiny bootstrap stub (`AVA_GATEWAY_URL` +
`AVA_MACHINE_*` + `AVA_CONFIG_SOURCE=gateway`) to join — the rest is pulled.

---

## 7. Deployment flow — bare machines → running cluster

Detailed commands in the [`deploy-ava-cluster` skill](../.agents/skills/deploy-ava-cluster/SKILL.md) §"Split across
machines" and [`dev-setup.md`](../conventions/dev-setup.md) §"First time bringing
up a new dev / agent-runner". The **shape** (what must happen in what order, and why):

1. **Join the tailnet first.** Every node + the operator's devices. WSL2 must
   install Tailscale *inside the distro* (it does not inherit the Windows host's
   identity). The gateway is reachable only here.
2. **Gateway host.** `install.sh --role gateway` → fill `~/.ava/.env` with
   `AVA_MULTIHOST_ENABLED=true`, `AVA_MACHINE_HOST=<tailnet addr>`, and data-plane
   URLs pointing at that address → `ava start --machine-name <gw>
   --serve-gateway`. This binds pg/redis `0.0.0.0`, writes the Tailscale-trust pg_hba /
   redis blocks, brings up the gateway + daemons, and UPSERTs the gateway's
   `machines` row. **macOS extra step**: allow the Homebrew `postgres` /
   `redis-server` binaries through the Application Firewall (binding `0.0.0.0` is
   necessary but not sufficient — the deploy skill has the `socketfilterfw` commands).
3. **Each agent-runner.** `install.sh --role agent-runner` → `ava enroll
   --gateway http://<gw-tailnet>:8000 --machine-name <name>` (writes the bootstrap
   stub; `--ssl-cert-file` if behind a corp TLS-MITM proxy) → `ava start`. The
   runner fetches its config from `/api/bootstrap`, brings up ops / restarter /
   watchdog, and UPSERTs its `machines` row advertising `http://<runner-tailnet>:
   <ops_port>`.
4. **Verify reachability both ways** (the NOT-automatically-tested boundary): the
   runner can reach pg `:5433` / redis `:6380` / gateway `:8000`, *and* the
   gateway can reach the runner's `<ops_port>` (`ava cluster status` round-trips a
   `status_probe` op to each runner). A one-way firewall hole is the classic
   split-deploy failure.

**Startup ordering is strict**: gateway before runners (agents are spawned only
via the gateway's `POST /api/agents`, which dials the target runner's ops
server). A runner that boots before the gateway just retries its bootstrap fetch.

---

## 8. Failure modes & recovery

### 8.1 A node goes offline

| Who is down | Symptom | Behavior / recovery |
|---|---|---|
| **An agent-runner** | gateway's dial to its ops server times out | `dispatch_to_machine` raises `ClusterOpUnreachable`; spawn-to-that-host fails fast, `ava cluster status` shows it unreachable. The cluster keeps running on the other runners. Its agents' processes are gone until it returns; `agents_meta` rows persist (lifecycle is in the DB, not the process). |
| **The gateway** | runners can't reach pg/redis/`:8000` | **Hard outage.** Runners can't claim inbound, the SDK 503s, the frontend can't load. Agents mid-turn lose their DB; on gateway return they resume from the LangGraph checkpoint. No failover (single data plane by design — non-goal: HA). |
| **A user device** | — | No cluster impact; it is only a client. |

`last_seen_at` is stamped at `ava start`, **not** a heartbeat — "online" is
decided by the live dial, never by a stale timestamp. A host announcing an
intentional stop POSTs `/api/cluster/stopping` so the roster shows "stopped" vs
"offline" (a crash a probe can't distinguish).

### 8.2 Rolling update across machines (built)

The three-phase `ava update` orchestration (`gateway/cluster.py`,
[`runbook.md`](../conventions/runbook.md)) is the load-bearing multi-machine flow,
hardened by three recovery layers — all **already landed**:

- **Phase A** — gateway pauses every runner (`cluster_stop` op: touch
  `$AVA_HOME/cluster_paused`, kill its restarter; watchdog skips reconcile while
  paused). **Pin target_sha once** so every node force-checks-out the same commit
  (no per-node `git pull` re-resolving a moving tip — the 2026-06-01 collision).
- **Phase B** — gateway migrates locally, then fans out `cluster_update` with the
  pinned sha; each runner force-checks-out, `uv sync`, `ava restart`, and clears
  its own pause.
- **Layer 1** — compensating unpause: any aborting exit dials `cluster_resume` to
  every paused host (`try/finally`).
- **Layer 2** — rollback-to-last-known-good: a failed gateway pull/sync/start
  rolls schema down (`_DOWN_FLOOR=22`) + `git reset --hard` to the pre-pull sha.
  Pre-Golden snapshots can't roll back → alerts MANUAL INTERVENTION.
- **Layer 2.5** — watchdog self-unpause: a `cluster_paused` flag outliving 10 min
  with no live update lock is auto-cleared (catches a hard-killed orchestrator).

Detail + the failure matrix: `cli/commands/update.py` + `cli/commands/_update_recover.py`.

### 8.3 Still to design

- **Tailnet partition** (a runner reachable for pg but not for `/ops`, or vice
  versa). Today each surface fails independently; there is no unified "this node
  is half-partitioned" state. The recovery layers above handle a *paused* node,
  not a *split* one. Lower priority — a tailnet rarely half-partitions — but
  noted.
- **Fail-fast on SHA-drift** ([`commit-pinned-cluster.md`](../future/infra/commit-pinned-cluster.md)):
  the `cluster_pin` is persisted + visualized, but a drifted node does **not**
  yet refuse work. That enforcement is the deferred load-bearing half — it makes
  "are these two nodes compatible?" disappear instead of being judged.
- **Per-machine PG role revocation** (§5.3) — today decommissioning a runner is
  delete-its-`machines`-row; the shared pg credential stays valid on it.

---

## 9. Migration path: single-box → split, and back

> ⚠️ **The flag steps below no longer exist.** `AVA_MULTIHOST_ENABLED` was deleted
> 2026-06-15: pg/redis always bind loopback + `AVA_MACHINE_HOST`, `/api/bootstrap`
> is always served and always authenticated, and the roster is always on. Going
> split is now just step 2 (stand up a runner and `ava enroll`); reversal is just
> dropping its `machines` row. The *reason* the round-trip is safe — cluster
> identity is untouched by topology, so the data plane never migrates — is what
> this section is kept for.

Single-box and a split gateway host are the **same unit** — `gateway,agent-runner`
vs `gateway`. Per §2 they are the same code on the same path; the migration is
additive and reversible:

1. On the single box, flip `AVA_MULTIHOST_ENABLED=true` and restart (this
   re-binds pg/redis to `0.0.0.0` + the Tailscale-trust blocks, and turns on
   `/api/bootstrap`). The box keeps running every agent locally — nothing else
   changes yet. (Its agent-runner half already dials the gateway over loopback —
   the gateway box's configured self-URL — so there is no path switch here; only
   the bind/auth surface widens.)
2. Stand up a new agent-runner (§7 step 3). Now agents can be spawned on either
   host (`spawn(machine=...)`).
3. *(Optional)* shed the agent-runner capability from the box to make it
   gateway-only: set `--no-serve-agent-runner` (or `machine_serve_agent_runner`
   to `false`), move agent workload to runners. Not required — a single-box host
   that also gained runners is a valid shape.

**Reversal**: drop the runners (`DELETE /api/cluster/machines/<name>` clears the
stale row), flip the flag off, restart → pg/redis re-bind loopback,
`/api/bootstrap` 404s, the box is a single-box again. With §10.B fixed,
`list_agent_runners` no longer changes its query by flag — a dropped runner is
simply an unreachable row the update fan-out skips at dial time, not a row hidden
by query scope.

Because cluster identity (db name, per-cluster instance ports) is unchanged by the
flag, the data plane survives the round-trip untouched — no migration, no data
move. This reversibility is what makes the opt-in cheap to try.

---

## 10. The restart ladder — the load-bearing sequencing decision

> **Outcome: Rungs 0–2 all landed, and the flag they were sequenced around was
> deleted 2026-06-15.** Rung 3 (per-machine credentials) and the TLS rung are
> still unbuilt — TLS is tracked in
> [`../future/infra/auth-tls-design.md`](../future/infra/auth-tls-design.md).
> Item E (fail-fast on commit-pin drift) is also still open, in
> [`../future/infra/commit-pinned-cluster.md`](../future/infra/commit-pinned-cluster.md).

The restart (2026-06-14) climbs a ladder, each rung building on the last. Target
for this pass: **Rung 2 — multi-host safe to opt into, behind the flag.** Rung 3
and TLS are roadmapped but not this pass's goal; the original A–G items are
absorbed into the rungs (the two consistency fixes keep their **A** / **B**
identity, which §2/§4/§9 reference).

- **Rung 0 — consistency (item B).** Item A (a `gateway_api_base()` localhost
  branch) is resolved: `gateway_api_base()` is role-blind and a gateway box's
  self-URL is loopback-by-config (§2), so there is no branch to collapse. The
  remaining single-vs-multi branch is `list_agent_runners()`'s query scope (B);
  collapse it so single-box and split run one path. No new capability, no
  exposure change. Detail in the **B** entry below.

- **Rung 1 — multi-host works again, de-Tailscaled.** Flip the flag, enroll a
  second host, verify the plumbing end-to-end on a trusted overlay. Two cheap
  ride-alongs that are pure improvements:
  - **Bind the resolved overlay interface, not `0.0.0.0`.** `_cluster_instance.py`
    binds pg/redis to `reachable_host()` rather than every interface, so a same-LAN
    neighbour cannot reach the data plane at all (closes the acute
    redis-with-no-source-filter hole, §5.1). macOS keeps its loopback default
    where the firewall blocks the overlay interface.
  - **De-hardcode the trust ranges.** Replace the literal Tailscale CGNAT blocks
    in `_pg_hba_body()` with a config field (`AVA_TRUSTED_CIDRS`, default
    loopback only). The core stops naming Tailscale; the overlay becomes the one
    the operator happens to use. **Pain point #1 is structurally gone here** — no
    Tailscale assumption survives in core code.

  Rung 1 is a **de-risking checkpoint, not a form to dogfood long**: inside the
  overlay it is still reachability == trust. Pass through it to Rung 2.

- **Rung 2 — single cluster join secret (closes the LAN pain point).** One
  shared secret, operator-supplied out-of-band at enroll (§5.3): the control
  plane (bootstrap / `/ops` / spawn) requires it; pg moves from `trust` to
  `scram-sha-256` with a derived password; redis gets `requirepass`. After
  Rung 2 the flag is safe to opt into over any encrypting transport — tailnet
  membership no longer equals cluster ownership. **This is the terminal rung for
  the restart.**

- **Rung 3 — per-machine credentials (refinement, roadmapped not targeted).**
  Mint a per-machine key + per-machine Postgres role at enroll (absorbs the full
  of the old item **C** + item **D** + bootstrap secret scoping **G**, §5.3), so
  decommissioning or a leak revokes one host without churning the others.

- **Later — app-layer TLS (Phase 2).** Only when a runner must live on a raw /
  untrusted LAN or public WAN with no encrypting overlay (gateway HTTPS, pg
  `sslmode`, redis TLS). Rung 2's per-request credential already leaves the seam:
  TLS is "add a cert + switch the scheme", not a re-architecture. Parked until a
  real raw-network need (operator chose "overlay first, keep the TLS interface
  open", 2026-06-14).

**Independent of the ladder (ship anytime):**

- **E. Fail-fast on commit-pin drift** (§8.3) — turns version-compat from a
  judgement into an invariant.
- **F. Tailscale ACL tightening** (§5.2) — least-privilege between tags; pure
  tailnet policy, zero code, independent of every rung.

### A. Gateway box self-call routing (§2 rule 4) — RESOLVED (2026-06-27)

`gateway_api_base()` is role-blind: it resolves the configured `AVA_GATEWAY_URL`
(env > `$AVA_HOME/gateway_url` file) for every caller, with no `is_gateway()`
branch. A gateway box's self-URL is **loopback by config** — `derive_env`
materializes `AVA_GATEWAY_URL=http://localhost:<gateway_port>` into every cluster's
`.env` at birth, so a box reaches its own gateway over loopback; a remote
agent-runner's `.env` carries the gateway's reachable URL, set at
`ava enroll --gateway`. The reachable address is a remote-only concern, never the
gateway's own self-URL. `ava start` reads `AVA_GATEWAY_URL` from `.env` with no
runtime default; on a gateway host it also prints its reachable address so an
operator can enroll runners against it.

This **reverses** the earlier plan here (single-box self-calls should traverse the
box's own reachable/tailnet address for path-uniformity). That plan rested on "the
box can hairpin to its own tailnet IP" — which is **false on macOS**: a host cannot
connect to its own Tailscale IP (the connect times out), even though remote peers
reach that same address fine. Loopback is the correct, dependency-free same-machine
path; routing self-calls over the reachable address bought nothing but a fragile
dependency. The watchdog *health probe* stays on its separate
`AVA_GATEWAY_HEALTH_URL` (also loopback, host-local supervision).

### B. Fix `list_agent_runners()`'s query-scope branch (§2 rule 4) — Rung 0

`shared/machines.py:list_agent_runners()` narrows to `AND name = <this host>`
when `multihost_enabled=false`. Remove the branch: always query all
agent-runners. The flag should gate **binding/auth**, not **query scoping**. The
stale-offline-row concern the branch was protecting against is mostly handled by
the live dial already — Phase A of the update fan-out classifies an unreachable
host as warn-and-skip, not abort (`cli/commands/update.py:_print_fan_out_results`;
only an op that ran and *reported* failure aborts). This makes update fan-out,
status, and roster one path regardless of the flag.

One verified cost the bare branch-removal would expose (2026-06-11): the Phase-B
poll (`_poll_until_unpaused`) retries **every listed host** until
`_POLL_TIMEOUT_S` (120s), so a stale offline row costs up to 120s of wall-clock
plus a spurious "degraded" warning per update. Pair the removal with two
uniform-path semantic filters (neither is a flag branch):

- **Skip `stopped_at IS NOT NULL` rows in the SELECT** — a host that announced
  an intentional stop is not a fan-out target; `register_self()` clears the
  marker when it returns, and the watchdog catch-up re-triggers its update.
- **Narrow the poll to hosts whose Phase-B spawn acked ok** — a host that never
  received the op cannot become unpaused; its catch-up path is the watchdog,
  exactly as the Phase-A skip message already promises.

Truly stale rows (a crashed host that never deregistered) remain a hygiene
matter for `DELETE /api/cluster/machines/<name>` (§9).

The security items the old A–G list spelled out — machine keys + authenticated
enroll/`/ops`/`/api/bootstrap` (old **C**), per-machine Postgres roles (old
**D**), bootstrap secret scoping (old **G**) — are now sequenced into Rung 2
(the single-secret form) and Rung 3 (the per-machine form); see §5.3. They are no
longer a separate "keystone" list because auth is the foundation, not a coda.

> **Considered, rejected for now**: extracting multi-host into a plugin — the deep
> ends (spawn routing, the three-phase orchestration, the ops server) are core
> gateway/CLI code; ~12-16 PRs vs the flag's ~4, for no near-term gain. The flag
> does not preclude a later extraction.

Until Rung 2 lands, the posture is honest and bounded: **opt-in, tailnet-only,
one operator, full mutual trust inside the mesh.** A deliberate, stated trade-off
— not an oversight — and the flag is what keeps it from leaking into the
single-box default. Rung 0 (A + B) should land regardless of the flag's future,
because it is the §2 principle made true in code.

---

## Related

- [`okf/index.ava.okf.md`](../okf/index.ava.okf.md) — built process shape, the gateway/ops dial, keep-alive
- `shared/config/` — the per-field `scope` model this builds on
- [`runbook.md`](../conventions/runbook.md) · [`dev-setup.md`](../conventions/dev-setup.md) — concrete bring-up + roster
- [`commit-pinned-cluster.md`](../future/infra/commit-pinned-cluster.md) — version-consistency foundation
- `cli/commands/update.py` · `cli/commands/_update_recover.py` — the recovery foundation

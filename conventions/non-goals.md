# Things deliberately not done

Things explicitly **not done** at the current stage, and their respective trigger conditions. Prevents "just adding it in passing because it's easy"
from blowing up the scope. Adding items is easier than removing them — when you see something getting built, come here
first and ask "what changed that made it worth doing".

- **Framework-layer "patch agent mistakes" plugins / hooks**: dangerous API blacklist,
  SyntaxError self-rescue, retry budget, permission checks, model fallback,
  model retry budget, etc. — designs where the plugin layer babysits the model — trigger: basically
  won't do. Reasons: (a) retry is resolved inside the tool function; (b) dangerous APIs are handled by
  sandbox + tool-internal hardcoding (e.g. SDK hardcoded to draft-only); (c) with a strong model,
  fail-fast lets the agent see the error and fix it itself, which beats plugin shimming.
- **Runtime model routing / fallback as an error-recovery shim or opaque cost/load router**
  (today's single-user positioning): no framework-level mechanism swaps an agent's model mid-run to
  paper over a provider error, or picks one via an opaque/random cost-or-load router — trigger: not
  for a single-user deployment; this is scoped to today's positioning, not a permanent architectural
  no (carve-out below). Reasons: (a) a mid-run model swap invalidates the provider's prompt cache
  (DeepSeek's server-side auto cache today; Anthropic `cache_control` once adopted, see below),
  undoing the exact savings routing would chase; (b) a hidden runtime router that silently picks the
  model for you is the same "plugin babysits the model" indirection already rejected in the item
  above for model-fallback / model-retry-budget plugins — opaque, hard to audit turn-to-turn, and
  today a single operator is present and can just swap config instead; (c) the right decision-maker
  already has the context — `ava.agents.spawn(config_overlay={"llm_model": ...})`, saved presets
  (`ava/agents/presets.py`), and the `ava-guide/models` skill's tier judgment put the model choice
  in the hands of whoever is deciding the sub-task, once, at spawn, not a framework router with none
  of that context. **This is not a rejection of multi-model support**: the registry
  (`shared/lm/registry.py`) backs 8 providers side by side, each with its own per-model tuning
  (`decisions/2026-07-25-per-model-config-registry.md`) — "single model" language elsewhere
  describes today's default *operating* configuration (one operator, one provider live per
  deployment), not a registry limit. What is rejected here is a dispatcher choosing FOR the agent
  inside a turn boundary as a mistake-shim or an opaque router. **Carve-out**: at open-source /
  multi-tenant scale, an ordered provider fallback chain becomes a real *availability* mechanism (no
  operator present to swap on an outage) — a different problem from today's mistake-shim, already
  scoped out of this non-goal in
  [`future/roadmap/open-source-prerequisites.md`](../future/roadmap/open-source-prerequisites.md)
  ("Provider fallback chain"). See `decisions/2026-07-29-no-runtime-model-routing.md`.
- **Cross-agent atomic rollback**: users rarely use rollback in practice (code version drifting from message
  history confuses the agent); use cancel + resend / fork instead — trigger:
  a real use case appears that needs "reset multiple agents together to a point in time", and fork
  can't solve it
- **Sync `ava.agents.spawn` API**: only async fire-and-forget is provided — trigger:
  the actual use case "spawn and immediately wait for a single sub-agent's result" pattern repeatedly surfaces and
  explicit wait inbound is too tedious to write
- **Cascade cancel/terminate**: killing the parent does **not** automatically kill children; the admin UI
  provides an explicit "kill subtree" — trigger: orphan accumulation becomes a problem
- **Supervisor as a separate process**: lifecycle lives on the gateway's lifecycle
  endpoints, no separate process — trigger: gateway's lifecycle workload grows
  large enough to affect UI HTTP response latency
- **Sandbox**: V1 runs bare on local; future firecracker / gvisor / seccomp — trigger:
  agent starts modifying its own code (V2), or the agent runs untrusted third-party input
- **Per-agent code-execution sandboxing as the general security boundary**:
  `execute_code` runs the agent's generated Python in the agent process on the
  host — no seccomp, no gVisor, no per-call sandbox around any single agent's
  turn. The `before_exec` hook (`demos/permission-hooks/`) is a pattern-matching
  mitigation, not a boundary (see the `ava/security.py` docstring). Isolation is
  a deployment decision — a dedicated OS user, machine, or VM per cluster — not
  a runtime property Ava enforces around the code it executes. This is
  deliberate, not debt, for the same reason auth is not (see "Auth /
  multi-user" below): the actor a sandbox would defend against is the same
  actor holding `execute_code`, so a boundary drawn *inside* the process only
  ever constrains a weaker copy of an actor already on the strong side of it.
  Trigger: this does **not** wait on the disposable-container work
  ([`roadmap/docker-sandbox.md`](../future/roadmap/docker-sandbox.md)) — that
  substrate isolates a whole throwaway *cluster* for eval / self-code-evolution
  workloads, it is not a general per-agent micro-sandbox for ordinary
  `execute_code` calls. Add one only if a use case appears where a single
  cluster must run agents against each other at different trust levels.
- **Terminal UI (TUI)**: the product surface is the Next.js fleet console
  ([`ui/web/web.ava.okf.md`](../ui/web/web.ava.okf.md)) plus chat
  channels (e.g. X, wired through `ava mcp install`) for talking to
  individual agents — not a terminal. Ava is an always-on, multi-agent fleet:
  supervision means seeing which agents exist, their health, the spawn/fork/
  message graph between them, and task tracking across possibly many
  concurrent agents — state that does not compress into a terminal's
  single-pane model without dropping most of what supervision needs. A TUI
  good enough to actually supervise a fleet ends up reinventing a windowing
  system inside a terminal emulator, which is strictly worse than the browser
  that already provides one. Trigger: basically won't do — this only becomes
  worth building if Ava's primary usage shifts from an always-on fleet back to
  a single foreground CLI session, which is a different product than the one
  this repo builds. (`ava status` / `ava cluster status` remain simple
  one-shot ops commands; this non-goal is about a persistent TUI, not those.)
- **Self-CODE-evolution**: agent modifies its own *code* — `ava.*`, the kernel, the
  system-prompt-*building* code — trigger: evaluation harness (SWE-bench + GAIA subset)
  + the disposable-container sandbox land first; without both, no go. The fuse is real
  *for code*: self-generated code needs a blast-radius boundary, and a self-code-merge
  needs an objective scorecard. See [`roadmap/self-code-evolution.md`](../future/roadmap/self-code-evolution.md).
  (This is the narrowed successor to the earlier blanket "Self-evolution: agent modifies
  `ava.*` / system prompt / policy files" no — that wording lumped code together with
  *text*, which the next item splits out.)
- **Self-evolution of skill/memory *text* is NOT a non-goal** (active roadmap item,
  [`roadmap/autonomous-learning-loop.md`](../future/roadmap/autonomous-learning-loop.md)):
  an agent curating its own `~/.ava/skills/` + memory-pool markdown executes no new code
  and is `git revert`-reversible, so the sandbox/eval fuse above does not apply — the
  safety property is reversibility, not confinement. Listed here so the split from the
  code fence is explicit, not so the loop is rejected.
- ~~**Fork point table**~~: dropped. Users rarely use rollback in practice (see "Cross-agent
  atomic rollback" above); fork is implemented via a new agent + `fork_source_agent_id` /
  `fork_source_checkpoint_id` fields on the agents_meta table for lineage tracking +
  recursive CTE to copy the LangGraph checkpoint chain (see
  agent-equals-thread design record in git); no separate
  fork_point table needed
- **Agent containerization**: V1 runs bare on host — trigger: deploying to a non-dev machine, or introducing
  a sandbox layer (linked to the "Sandbox" item)
- **Agent cold-start parallelization beyond the MCP layer (fork-from-warm / zygote)**: an agent boots by a
  fresh `python -m agent` exec (~1.1s cold import — ~1400 modules, single-threaded, the pydantic
  core-schema build dominates) as a detached native session. The MCP daemon's *second* cold start is now
  overlapped with the rest of boot (#62); past that, a resident pre-warmed template process that `fork()`s
  per spawn to skip the import tax is **not done** — trigger: an actual workload appears where per-agent
  boot latency accumulates into a *caller's* wait. Reason it doesn't today: spawn/resurrect is a
  fire-and-forget DB insert — the caller (e.g. an agent fanning out 100 `send_message`s) never waits on
  bring-up, and the gateway brings agents up asynchronously and independently, so one agent's 1-2s boot
  never sums into the caller's latency. Against that flat cost curve zygote's price is steep: a forked child
  still has to be tracked and reaped exactly like the current detached spawn (pid record + init reparenting),
  so all it buys back is the ~1-2s import cost the async, independent bring-up already keeps off the caller's
  latency path — while adding fork-state-sharing complexity (macOS has no COW zygote for a running
  interpreter). The cost structure does not support it.
- **Anthropic prompt caching**: `cache_control` on system prompt + long prefix —
  trigger: when switching to Anthropic as the gateway model, add this at the same time (doing the plumbing alongside the model API
  evolution is more cost-effective at that point). Currently DeepSeek's server-side auto cache is observably sufficient
- **Auth / multi-user**: no login. The gateway binds all interfaces but is reachable
  **only over the private network** (the gateway host has no public IP), and many
  users will just run the whole thing on one machine (localhost) — so the **trust
  boundary is the user's own machine / single-user private network**, and the cluster runs
  unauthenticated. This is deliberate, not debt, and the reasoning is sharper than "the
  private network is trusted": the agent itself holds `execute_code` (= arbitrary bash + the full
  SDK), so it is the most powerful actor *inside* the boundary — gateway auth can neither
  restrain it nor needs to. Per-caller auth only buys something against actors *outside*
  the boundary (a second person on a shared private network; the public internet), which do not
  exist in the single-user model. **Corollary** (this is why the reduce-compare page
  callback is fine): the wide-open CORS (`allow_origins=["*"]`) and the unauthenticated
  page→agent callback (an agent-built page's browser JS POSTing to
  `/api/agents/{id}/messages`) add no real surface — a prompt-injected agent page is
  strictly *weaker* than the agent's own bash, so it grants nothing the agent couldn't
  already do directly; the one thing CORS/auth would defend (a public web origin bridged
  in through the user's browser) presupposes the gateway being reachable off the private
  network, which is exactly the trigger. **Trigger** — and only then add per-caller gateway
  auth *and* lock CORS off `*`: exposing the gateway beyond the private network (a public tunnel /
  open port), or onboarding distinct users who need separate identities/permissions.
  Before that, adding auth is machinery for a threat that does not exist. The earlier
  "`127.0.0.1` bind" framing is retired: the move to private-network-only + no-auth shipped
  2026-06-05.
- **Agent permission verification / Gateway routing of SDK calls**: at current SDK shape, a lineage
  check (e.g. which peers an agent may act on) to prevent agent misoperation is sufficient; the real security boundary
  only makes sense with a sandbox. Changing SDK calls from "agent code directly SQL" to "agent code
  → HTTP → Gateway → SQL" adds latency + complexity but **does not** add defensive power — agent code
  is in the same process as the main agent process (in-process worker thread), can directly modify
  `ava.self.AGENT_ID`, can read process env, any token / identity can be bypassed. Trigger:
  sandbox lands (firecracker / gvisor / container / different UID), token-based auth
  + Gateway routing landing together with sandbox is the real boundary. Before then, rely only on SDK shape +
  controlled-task assumptions
- **Cross-machine wire-format rolling-upgrade compat windows**: when a
  cross-machine wire shape changes (e.g. the `/ops` result failure
  payload), do **not** keep a parallel parse branch for the old
  shape "just in case some agent-runner hasn't upgraded yet". Trigger:
  fleet grows large enough that not every host can `ava cluster update`
  together in the same hour. Reason: the fleet today is 1 gateway + 1-2
  agent-runners, and `ava cluster update` fans out within minutes — every
  rolling-upgrade compat branch shipped so far (#437) was deleted the
  same day (#444). Compat code at fleet size 1-2 is dead-on-arrival
  investment that confuses the next reviewer.
- **Monitor mechanism dashboard / metrics pipeline / anomaly detection**:
  monitoring window decisions use polling loop + threshold rules; do not introduce Prometheus / Grafana / statsd /
  ML anomaly detection. Trigger: system grows large enough that polling via
  reading the agents' log files + `get_status()` alone can't keep up, or cross-machine monitoring is needed; then
  bring in a metrics pipeline. See
  self-rolling-release design record in git).
  (Distributed *tracing* is no longer on this list: `shared/trace.py` records
  vendor-neutral OTLP/JSON spans to a local mirror — Traceloop/OpenLLMetry
  auto-instruments the LLM/tool path; `ava trace ship` replays the mirror to a
  self-hosted viewer out-of-band. That is span recording, not a metrics pipeline;
  the metrics/dashboard/anomaly rejection above still stands.)

- **Folding the flat `shared/` (~50 files) / `scripts/` (~27 files) dirs into subpackages**
  (proposed groupings `cluster_*` / `machine_*` / `plugin_*`, or scripts by lint/devops/delete):
  not done — keep them flat. Trigger: a candidate group develops genuine internal cohesion that a
  package `__init__` could hide behind a narrow surface. Reasons: (a) a "deep module" (Ousterhout) is
  a narrow interface over a thick implementation — that's the interface/impl ratio of one module,
  **orthogonal to directory depth**; the pre-2026-08 `shared/cluster.py` was already deep, and
  moving it to `shared/cluster/core.py` would have changed only the import path, not its depth
  (the actual 2026-08 split into `shared/cluster/{registry,ports,derive,provision}.py` was
  line-count + cohesion driven — the package `__init__` preserved the single import surface).
  50 flat files can be 50 deep
  leaves. (b) Folder grouping only adds depth if the package hides internal files; empirically the
  candidate groups have **zero internal cohesion and wide independent external surfaces** (each
  `cluster_*` / `plugin_*` module is imported directly by different external consumers), so wrapping
  them yields a pass-through barrel (a *shallow* module) + churn across all import sites, depth gain
  zero. (c) `scripts/` has no `from scripts.X import` anywhere — pure standalone entrypoints; the
  `lint_*` prefix already groups them; triage found no dead scripts. A **file-count / subfolder-count
  lint** was rejected for the same reason: it's ownerless accumulation = Sweeper territory, not a
  commit-blocking wall (see [`lint-vs-sweeper.md`](lint-vs-sweeper.md)). General rule: when a
  restructure is proposed off a file-count or folder-shape proxy, push back — the real axis is
  per-module interface width and genuine cohesion, not folder shape.

- ~~**Add observability columns to `agents` table (terminated_at / total_turns / token usage etc.)**~~:
  done by event sourcing path (2026-05-07). No new observability columns; new observability dimensions =
  add new `kind=` values + `attributes` fields to the unified `events` table, schema unchanged.

- **Bespoke spawn-wrapping endpoint per "AI does X for you" affordance**: a dedicated route whose
  only job is to assemble a prompt and forward it to spawn — `/api/presets/draft` was this, folded
  away (2026-07-30) in favor of composing the prompt client-side. The affordance is already
  expressible with existing primitives: `POST /api/agents` plus the frontend composing a short
  first-message prompt and navigating to the new agent's chat; the domain knowledge of *how* to do
  the task lives in a skill the prompt points at, not in a server-side prompt template. Trigger: a
  case needs machinery a plain composed prompt can't express — e.g. the `packages` / `schedules` /
  `guide` draft endpoints deliberately restrict which fields a caller may pass (no URL/spec fields)
  to keep candidate-judgment inside the agent's own conversation; that is a different design point
  from plain prompt-templating and is evaluated on its own merits, not exempted by this non-goal.

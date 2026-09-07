# Ava Fleet Heartbeat — Design

> Status: **Partially superseded** (2026-06-22). The self-check heartbeat (Tier 2)
> is now **implemented with a deliberately simpler design** than this proposal:
> a single gateway daemon (`services/heartbeat/`) that nudges idle agents on a
> fixed interval, with an agent-driven **opt-out** (`ava.self.pause_heartbeat`)
> instead of the exponential-backoff + miss-counter + escalation chain below.
> See [`runbook.md`](../../conventions/runbook.md) (the `heartbeat` service row) for current
> behavior and the git log (heartbeat-opt-out design)
> for why the simpler design won. This file is kept as the original research
> record (OpenClaw study + the rejected two-tier proposal). The Tier-1 liveness
> note in §2.2 already records its own partial landing via the restarter reaper.

## 0. Problem

The Ava fleet lacks a fallback liveness mechanism. If an agent forgets to
self-report and no watcher is set, every agent can sit idle simultaneously
with no one aware. The fleet lead only knows an agent is alive when it
produces activity — silence is ambiguous (idle? dead? stuck?).

This doc researches OpenClaw's heartbeat mechanism, then proposes a
two-tier heartbeat scheme for Ava: a **gateway liveness watchdog** (Tier 1)
and an **agent self-check heartbeat** (Tier 2).

---

## 1. OpenClaw Heartbeat — Research Summary

### 1.1 What it is

A periodic **agent turn** that runs inside the agent's session. Its purpose
is to surface anything needing attention (follow-ups, inbox checks,
reminders) without spamming the user. It is **not** a background task — it
does not create detached work records.

### 1.2 Trigger & schedule

- **Duration-based**, not cron: `30m`, `1h`, `2h30m`. Default: `30m` (or
  `1h` when OAuth/Claude CLI auth detected).
- Set `0m` to disable.
- Not approximate — the timer fires exactly at the configured interval
  (subject to deferral checks).

### 1.3 Deferral / skip logic

Before every run, multiple conditions can cause the heartbeat to be skipped:

| Condition | Behavior |
|---|---|
| Active cron job or queued cron work | Defers automatically |
| `skipWhenBusy: true` + subagent/nested lanes busy | Defers |
| `showOk`, `showAlerts`, `useIndicator` all disabled | Skips (`reason=alerts-disabled`) |
| `HEARTBEAT.md` exists but effectively empty | Skips (`reason=empty-heartbeat-file`) |
| `HEARTBEAT.md` tasks block with no due tasks | Skips (`reason=no-tasks-due`) |
| Outside `activeHours` window | Skips until next in-window tick |
| Main queue / target session lane busy | Skips, retried later |

### 1.4 Execution model

1. Scheduler fires → deferral checks → sends heartbeat prompt verbatim as
   user message to the model.
2. Model responds. System inspects reply for `HEARTBEAT_OK` token.
3. If reply ≤ `ackMaxChars` (default 300): acknowledged, no alert.
   If longer or missing `HEARTBEAT_OK`: treated as alert, delivered per
   visibility settings.
4. Runs in agent's main session (or `session` config), with full
   conversational context.
5. **Does not** keep session alive — idle expiry is based on real user
   interactions.
6. **Does not** create background task records.

### 1.5 Configuration surface

| Field | Type | Default | Purpose |
|---|---|---|---|
| `every` | duration | `"30m"` | Interval. `"0m"` = disabled |
| `model` | string | agent default | Model override for heartbeat turns |
| `includeReasoning` | bool | `false` | Deliver Thinking message alongside |
| `lightContext` | bool | `false` | Bootstrap only `HEARTBEAT.md`, skip other workspace files |
| `isolatedSession` | bool | `false` | Fresh session each run (no prior history) |
| `skipWhenBusy` | bool | `false` | Defer when subagent/nested lanes busy |
| `session` | string | `"main"` | Which session heartbeat runs in |
| `target` | string | `"none"` | Delivery target (`"last"`, `"none"`, channel id) |
| `directPolicy` | `"allow"`\|`"block"` | `"allow"` | Control DM delivery |
| `to` | string | — | Recipient override |
| `accountId` | string | — | Multi-account channel selection |
| `prompt` | string | (see below) | Override the heartbeat message |
| `ackMaxChars` | number | `300` | Max OK-ack length before alert |
| `suppressToolErrorWarnings` | bool | — | Suppress tool error warnings |
| `timeoutSeconds` | number | `min(every,600)` or global | Max seconds per heartbeat turn |
| `activeHours` | object | — | Time-window restriction |

Default prompt:
```
Read HEARTBEAT.md if it exists (workspace context). Follow it strictly.
Do not infer or repeat old tasks from prior chats. If nothing needs
attention, reply HEARTBEAT_OK.
```

Per-agent override: if **any** agent has a `heartbeat` block, **only those**
agents run heartbeats. Otherwise, the global default applies to all.

### 1.6 Failure handling

- **`onAgentError` event hook** — fires automatically on agent error during
  any automated task (heartbeat, cron, etc.). Configurable to notify
  operators.
- **Escalation rules** — encoded in `HEARTBEAT.md` itself (e.g., "if disk
  >90%, notify ops channel immediately").
- **Logs** — `openclaw logs --filter automation` for dedicated automation
  stream.
- **Cron list** — `openclaw cron list` shows scheduled jobs and last-run
  status.
- **Manual testing** — best practice: test heartbeat/cron behavior manually
  before activating.

### 1.7 Heartbeat vs Cron — when to use which

| Mechanism | Timing | Agent thinks? | Use case |
|---|---|---|---|
| **Heartbeat** | Approximate, flexible | Yes — assesses situation, decides action | Inbox monitoring, calendar checks, awareness tasks |
| **Cron** | Exact wall-clock | No — predetermined command/message | Reports, backups, deterministic automation |

---

## 2. Proposed Ava Heartbeat — Two-Tier Design

### 2.1 Tier separation rationale

OpenClaw's heartbeat conflates two concerns: "is my schedule due?" and "am
I alive?". For a single-agent system this is fine — the agent is either
alive (running turns) or the scheduler won't fire. In a **multi-agent
fleet**, these decouple:

| Concern | Question | Who monitors | What if silent |
|---|---|---|---|
| **Liveness** | Is the agent process alive? | Gateway | Escalate |
| **Schedule** | Does the agent have work to report? | Agent itself | Schedule fires when due |

**Tier 1** (liveness watchdog) is **structural** — the gateway already owns
process monitoring. This tier answers "did an agent die silently?"

**Tier 2** (self-check heartbeat) is **behavioral** — the agent wakes
periodically, checks its world, and reports. This tier answers "is there
anything the fleet lead should know?"

### 2.2 Tier 1 — Gateway Liveness Watchdog

> The original process-reaper proposal is superseded by agent-host ownership,
> durable pending-work scans and host health checks. Idle has no task or
> per-agent process to probe. The escalation proposal below is historical
> research, not the current heartbeat implementation; see
> `services/gateway_side/heartbeat.ava.okf.md` for the implemented contract.

#### Trigger

Fixed 5-minute interval. No exponential backoff — liveness is a binary
question with a predictable detection window.

The gateway already tracks `agents_meta.last_active_at` (updated on every
inbound message processed or turn completed). A new periodic job in the
gateway process scans for agents where:

```
now - last_active_at > heartbeat_timeout
AND status IN ('running', 'idling')
AND heartbeat_enabled != false
```

#### Work logic

Watchdog **does not pause** during agent work — if the agent is actively
running turns, `last_active_at` is continually refreshed, so the watchdog
never triggers. The "pause" is intrinsic: activity itself is the heartbeat.

#### Implementation

A lightweight async task inside the gateway process (no separate session
session). It polls `agents_meta` and compares `last_active_at` against the
configured timeout. This is a **DB read** not an agent message — it does not
consume LLM tokens.

```
┌────────────┐    poll every 5m    ┌───────────┐
│  Gateway   │ ─────────────────→ │  Postgres │
│  watchdog  │ ←── stale agents ── │           │
└────────────┘                    └───────────┘
       │
       │ escalate (resurrect / alert)
       ▼
  ┌──────────┐
  │ Fleet    │
  │ Lead /   │
  │ Human    │
  └──────────┘
```

#### Fallback behavior

```
Missed heartbeat count → action:

1 miss  (5 min)     → emit a WARNING event to the unified `events` stream
3 misses (15 min)   → attempt resurrect (once)
5 misses (25 min)   → log ERROR, send message to fleet lead agent
10 misses (50 min)  → escalate to human (push notification)
```

Anti-zombie guard: if an agent has been auto-resurrected >3 times in a 1-hour
window, stop resurrecting and escalate to human immediately.

#### Configuration

| Parameter | Default | Description |
|---|---|---|
| `heartbeat_timeout` | `"5m"` | How long before agent considered unresponsive |
| `heartbeat_max_resurrects_per_hour` | `3` | Anti-zombie guard |
| `heartbeat_enabled` | `true` | Per-agent disable (e.g., short-lived workers) |

### 2.3 Tier 2 — Agent Self-Check Heartbeat

This is the closer analogue to OpenClaw's heartbeat. The agent periodically
receives a system message that prompts it to check its world and report.

#### Trigger

**Exponential backoff** from minimum to maximum:

```
After last activity:
  5 min   → first check
 15 min   → second check
  1 hr    → third check
  6 hr    → fourth check
 24 hr    → fifth check and every 24 hr thereafter
```

Rationale: if an agent has been idle for 6 hours, a 5-minute heartbeat is
wasteful. But we want tight checking immediately after activity stops (the
"just finished something, should I report?" window), then relax.

The timer **resets on every agent turn** (any inbound message processed,
including user messages, peer messages, or Tier 2 heartbeats themselves).

#### Work logic

Heartbeat **only fires when agent is `idling`**. If the agent is `running`
(processing a turn), the timer pauses — the agent is demonstrably alive.
When the agent's status flips back to `idling`, the timer resumes from where
it left off.

This avoids interrupting work and avoids redundant checks.

#### Implementation

**Inbound message** — the gateway inserts a special inbound row of kind
`heartbeat` (new inbound kind). The agent's LangGraph loop picks it up like
any other message. The agent responds tersely (acknowledgment) or with a
status report.

The heartbeat message format:
```
[heartbeat #N] You have been idle for <duration>. Report anything the
fleet lead should know, or reply HEARTBEAT_OK.
```

New inbound kind `heartbeat` is distinguishable from `user` / `peer` /
`system` messages. The agent's system prompt includes a heartbeat response
convention: reply `HEARTBEAT_OK` to ack, or provide a brief status summary.

The gateway inserts the heartbeat inbound and sets a short timeout (e.g.,
2 minutes for the agent to respond). If no response within timeout, the
gateway increments the miss counter and falls through to Tier 1 escalation.

```
┌────────────┐   insert heartbeat inbound   ┌───────────┐
│  Gateway   │ ────────────────────────────→ │  Agent    │
│  scheduler │ ←── HEARTBEAT_OK or report ── │  session  │
└────────────┘                               └───────────┘
```

#### Fallback behavior

Tier 2 failures feed into Tier 1's escalation chain (since Tier 2 failure =
agent didn't process the inbound, which means it's effectively dead — Tier
1's `last_active_at` won't advance).

#### Configuration

| Parameter | Default | Description |
|---|---|---|
| `heartbeat_min_interval` | `"5m"` | First check after idle |
| `heartbeat_max_interval` | `"24h"` | Maximum backoff ceiling |
| `heartbeat_backoff_multiplier` | `3` | How aggressively to back off |
| `heartbeat_prompt` | (see above) | Override the heartbeat message |
| `heartbeat_timeout` | `"2m"` | Max wait for agent response |
| `heartbeat_enabled` | `true` | Per-agent override |

### 2.4 Configuration — Cluster default with per-agent override

Following OpenClaw's model:

```yaml
# Cluster-level default (in cluster config)
heartbeat:
  tier1:
    interval: "5m"
    max_resurrects_per_hour: 3
  tier2:
    min_interval: "5m"
    max_interval: "24h"
    backoff_multiplier: 3
    timeout: "2m"

# Per-agent override (in agent spawn config)
agents:
  - id: 10
    label: "fleet-lead"
    heartbeat:
      tier2:
        min_interval: "2m"   # fleet lead checks in faster
        max_interval: "1h"
  - id: 99
    label: "short-lived-worker"
    heartbeat:
      tier1:
        enabled: false       # don't watchdog ephemeral workers
      tier2:
        enabled: false
```

**Default**: all agents get cluster defaults. Any per-agent `heartbeat` block
overrides the corresponding fields (shallow merge).

---

## 3. Design Decisions — Per-Dimension Analysis

### 3.1 Trigger: Fixed vs Exponential Backoff

| | Fixed | Exponential Backoff |
|---|---|---|
| **Simplicity** | Simple to reason about | More complex state tracking |
| **Cost** | Wastes tokens when idle for days | Efficient for long-idle agents |
| **Detection speed** | Predictable worst-case | Can be fast when needed, slow when not |

**Decision**: **Split**. Tier 1 (liveness) uses fixed — predictability
matters more than cost. Tier 2 (self-check) uses exponential backoff —
the "did you finish your last task and have something to say?" question
loses value over hours of silence.

### 3.2 Work Logic: Pause During Work?

**Decision**: **Yes — activity is the heartbeat**. The timer resets on
every turn completion. Tier 1's `last_active_at` naturally reflects this.
Tier 2 only fires when `status = 'idling'`.

Why not OpenClaw-style `skipWhenBusy`? Because Ava's agent status is
explicit (`running` vs `idling`), making the check simpler: if `running`,
don't heartbeat. If `idling`, do.

### 3.3 Implementation: Session vs Inbound Message

| | Session | Inbound Message |
|---|---|---|
| **Existing infrastructure** | None for heartbeat | Full message pipeline exists |
| **Agent awareness** | Agent must poll session | Agent gets it as a turn |
| **Failure detection** | Session alive ≠ agent responsive | No response = miss detected |
| **Token cost** | Zero (external process) | Small (one turn) |
| **Complexity** | New session lifecycle | New inbound kind, minimal |

**Decision**: **Inbound message** for Tier 2. Tier 1 doesn't need either —
it's a gateway-internal DB poll.

The inbound message approach reuses the entire existing delivery pipeline
(gateway → SSE → agent claim → LangGraph node). A new inbound kind
`heartbeat` is the only new schema element. The agent sees it as a regular
turn and responds with a single reply.

### 3.4 Fallback: Escalation Chain

**Decision**: **Graduated escalation with anti-zombie guard**.

```
MISSES → ACTION
  1    → WARNING log
  3    → auto-resurrect
  5    → notify fleet lead agent (send_message)
 10    → human push notification
```

Anti-zombie: max 3 auto-resurrects per hour per agent. Beyond that,
escalate to human directly.

Why auto-resurrect at all? Because in practice, agent process death is
often transient (OOM, LLM API timeout, network blip). A single resurrect
attempt is cheap and often fixes the problem. But looping resurrects
(same error kills the agent repeatedly) is anti-helpful — hence the guard.

### 3.5 Configuration Granularity

**Decision**: **Cluster default + per-agent override** (same as OpenClaw).

- Cluster default: sensible for 90% of agents.
- Per-agent: fleet lead, orchestrators, and critical agents get tighter
  settings.
- Disable per-agent: short-lived workers spawned for a single task don't
  need heartbeat.

Why not per-agent only? Because "set it once and forget" is the right
default — most agents should have the same heartbeat policy, and the
cluster admin shouldn't need to configure it per agent.

---

## 4. Trade-off Analysis

### 4.1 Benefits

1. **No more silent deaths**: Agent process death detected within 5 minutes,
   auto-resurrect attempted within 15.
2. **Fleet lead stays informed**: Agents that finished work but haven't
   reported get a nudge at 5 min, 15 min, 1 hr.
3. **Low cost**: Tier 1 is a DB query (zero LLM tokens). Tier 2 is one
   short turn per idle agent per backoff interval (a few tokens for
   `HEARTBEAT_OK`).
4. **Reuses existing infra**: Inbound messages, agent status, resurrect
   endpoint, unified `events` logging — all already exist.
5. **Configurable escape hatch**: Per-agent disable for workers that
   shouldn't be watched.

### 4.2 Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Heartbeat spam drowns fleet lead | Only escalate after 5 misses (~25 min); use dedicated heartbeat event type for filtering |
| Zombie resurrect loop | Anti-zombie guard: max 3 auto-resurrects/hour |
| Gateway becomes SPOF for heartbeat | Already SPOF for fleet — heartbeat adds negligible load (one DB query/5 min) |
| Agent ignores heartbeat message | Same as ignoring any inbound — miss counter increments, escalation proceeds |
| Heartbeat wakes agent during important work | Tier 2 only fires when `idling`; Tier 1 never sends messages |
| False positives from long-running agent turns | `last_active_at` advances on every turn completion, even for multi-minute turns |

### 4.3 What We're NOT Doing (and Why)

| Not doing | Why |
|---|---|
| Dedicated heartbeat session per agent | Heavy. Inbound messages give the same signal with less infra. |
| Cron-based exact-timing heartbeat | Cron in Ava is for detached work; heartbeat is for session-bound awareness. Different mechanisms, different purposes. |
| Per-agent HEARTBEAT.md files | OpenClaw-ism that maps poorly to Ava's fleet model. Ava agents use `memory` for persistent context. A heartbeat instruction can live in the cluster config or agent config. |
| Heartbeat-only agents (dedicated watchdog agent) | Adds another agent to monitor, creating infinite regress. Gateway-level watchdog is simpler. |
| Real-time (sub-minute) heartbeat | Not needed for fleet health. 5-minute detection window is sufficient. If sub-minute is needed, that's a different problem (health-check endpoint, not agent heartbeat). |

---

## 5. Implementation Roadmap (Optional)

### Phase A — Foundation (1 PR)

- New inbound kind `heartbeat` in `shared/` schema + migration.
- Gateway watchdog async task (Tier 1): poll `agents_meta` every 5 min,
  log warnings on stale agents.
- Agent system prompt section: heartbeat response convention
  (`HEARTBEAT_OK` ack).
- Cluster config schema: `heartbeat` block with defaults.
- Tests: watchdog detects stale agent, logs warning.

### Phase B — Escalation (1 PR)

- Resurrect-on-miss logic with anti-zombie guard.
- Tier 1 → fleet lead notification pipeline.
- `events` entries for heartbeat misses and resurrects.
- Tests: resurrect fire, anti-zombie guard, escalation to fleet lead.

### Phase C — Tier 2 Self-Check (1-2 PRs)

- Gateway scheduler for Tier 2 heartbeats (exponential backoff).
- `heartbeat` inbound kind processing in agent loop.
- Agent response: `HEARTBEAT_OK` vs status report.
- Per-agent heartbeat config override.
- Tests: backoff schedule, timer reset on activity, response handling.

### Phase D — Observability (1 PR)

- Fleet view: heartbeat status indicator per agent (green/yellow/red).
- Heartbeat event filter in timeline.
- Dashboard stats: heartbeat miss rate, resurrect rate.

---

## 6. References

- OpenClaw Heartbeat docs: <https://docs.openclaw.ai/gateway/heartbeat>
- OpenClaw Automation overview: <https://docs.openclaw.ai/automation>
- OpenClaw Design Patterns (Part 3): <https://kenhuangus.substack.com/p/openclaw-design-patterns-part-3-of>
- Stanza: OpenClaw Heartbeat — Cron & Automation: <https://www.stanza.dev/concepts/openclaw-heartbeat-automation>
- Ava Fleet View: the design doc was deleted once the `/fleet` view shipped; read
  `ui/web/src/components/fleet/` + `gateway/routers/fleet_graph.py` instead.

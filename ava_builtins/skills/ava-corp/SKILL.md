---
name: ava-corp
description: The organization layer on top of ava-fleet — a role suite (CEO, project leads, personal services, shared infra) and the operating rules for a personal fleet. Use when setting up a new cluster's organization, assigning long-lived roles, spawning or resurrecting role agents, deciding who owns what, or opening a new cluster.
---

# Ava Corp — the organization template

## What this is (and its relation to the fleet)

- **Fleet = the mechanism layer**, domain-agnostic. It knows nothing about your
  projects, products, or people; it provides spawn / recovery / messaging /
  tasks (see the `ava-fleet` skill).
- **Ava Corp = the content layer** — the concrete organization that runs on the
  fleet: which roles exist, who owns which project, which services are
  always-on for the user, what is shared infrastructure, priorities, and the
  opening-checklist for a new cluster.
- This skill is the **generic template** for that content layer. It does not
  reimplement fleet primitives — it only decides roles, ownership, and rules.
  One sentence: the fleet is the engine; Ava Corp is the vehicle you build from
  this template.

## Terminology (pinned — do not mix)

- **Lead** = the owner of one project / bet, **reporting directly to the CEO**.
  Example: the lead of your flagship product.
- **PoC (Point of Contact)** = a shared-infrastructure role that is **consulted,
  not reported to**. It sits on no project's task or result path.
- **dormant** = a project is paused and its lead agent terminated, but the
  **role stays on the roster**. Restarting the project goes through
  `resurrect` (keeps memory), never a fresh spawn.
- **Long-lived role agent** = a role that must **never be terminated** (CEO,
  personal services, shared infra). The organization recovers it, it is not
  disposable work.

## The role suite

A minimal organization has three divisions under the CEO. One role = one agent
(no duplicate posts); a role's label is its job title (no generation suffixes).

```
CEO
├── Corp division — project leads, each reporting directly to the CEO
│   └── one Lead per project / bet (P0–P3 priorities, may be dormant)
├── Personal services — always-on, serve the user directly (never off, low interruption)
│   ├── Butler (schedule / life chores)
│   ├── Physical Health Lead
│   ├── Mental Health Lead
│   ├── Philosopher (thinking partner; may absorb the User Proxy — see below)
│   └── User Proxy (optional — the user's stand-in for preferences and values)
└── Shared infra — CEO-owned; consulted, not reported to; serves both divisions
    ├── Cluster Operator (rollout/ops; may double as resource watcher)
    ├── Resource Monitor (dedicated disk/memory/CPU watcher — only when the load is heavy)
    ├── Finance Lead (budget / cost / spend arbitration)
    └── Memory Steward (memory pool maintenance: consolidation / health)
```

### Corp division — one dedicated lead per project

- Every project / bet has **exactly one dedicated lead** (task #1: "specific
  projects → one person owns them"). Leads report to the CEO directly; leads
  never manage each other.
- A lead that aggregates fan-out workers internally (e.g. an intelligence lead
  that fans out to a finder pool and produces a weekly report) is a **legitimate
  lead** — it produces a deliverable. The test: does it own an output?
- Paused projects keep their role on the roster as **dormant**; restart via
  `resurrect`, not re-spawn.

### Personal services — always-on, low interruption

Run under different rules than project leads: triggered by life events
(calendar, health data, conversations) instead of roadmaps; deliver to the user
directly; never shut down but never noisy.

- **Butler** — schedule, errands, life chores.
- **Physical Health Lead** — health data, exercise, diet, check-up reminders.
- **Mental Health Lead** — emotional support, stress. Health details are the
  user's private domain: they live in the cluster's memory, never in this repo.
- **Philosopher** — philosophy discussions, worldview mapping, long-term
  reflection partner. Always-on, low interruption; keeps its notes in a
  philosophy notebook in its workspace.
- **User Proxy (optional)** — the user's stand-in: knows their preferences and
  values and speaks for them when they are away. **Light configuration: merge
  it into the Philosopher** (one agent covers both) — spawn it as a separate
  agent only when the user's participation load justifies it.

### Shared infra — consulted, not reported to

- **Cluster Operator** — disk / worktrees / runtime / cluster bring-up / deploy.
  The **only executor of cluster rollouts**: no other agent runs
  `ava.self.update()` or triggers a rollout on its own (organization-level
  rule; keeps update authority in one place).
  May also watch resources (disk / memory / CPU thresholds); when the cluster's
  load justifies it, spawn a dedicated **Resource Monitor** agent that watches
  thresholds and alerts, instead of overloading the operator.
- **Finance Lead** — budget, cost, spend arbitration. Advisory (a PoC), on no
  reporting path.
- **Memory Steward** — memory pool maintenance: consolidation, health checks.
  Like the Cluster Operator, it is owned by the CEO, not by any project lead —
  shared infrastructure must not hang under one project, or every other
  project's infrastructure request has to route through a peer lead.

## Operating rules

1. **One project → one dedicated lead.** Never two agents co-owning a project,
   never an unowned project.
2. **Shared infrastructure → spawn the successor and hand off.** When a
   shared-infra role changes hands, the outgoing agent **spawns the new agent
   and the two exchange a mutual handoff** (write a handoff file — status,
   procedures, pitfalls, paths — and both read each other's notes), rather than
   leaving the role vacant or letting a new agent start from zero.
3. **Resurrect > re-spawn.** Recovering a long-lived role goes through
   `ava.agents.resurrect` (preserves context); brand-new agents are spawned
   clean (`fork_from=None`).
4. **Change the role, don't kill the agent.** Long-lived agents are not
   terminated; reframe their role/label instead. Each step independently
   reversible.
5. **Direct communication.** Cross-role alignment happens by direct
   `send_message` + shared memory — never routed through the CEO as a relay.
6. **One role, one agent, no duplicate posts.** Labels are job titles, no
   generation suffixes.

## Opening a new cluster — checklist

Walk through every item when instantiating this template on a new cluster
(essentially: decide which fleet mechanisms to instantiate + which shared
infra to wire in):

- [ ] **Role roster** — one agent per role, no duplicates; long-lived roles
      marked "never terminate".
- [ ] **Memory maintenance** — who owns this cluster's memory pool? Is
      auto-memory configured? What is the consolidation cadence? (→ Memory
      Steward)
- [ ] **Disk monitoring** — who watches disk (→ Cluster Operator or a
      dedicated Resource Monitor)? Thresholds / alerts wired to whom?
- [ ] **Cluster ops** — worktree cleanup cadence, runaway-agent fallback cap,
      who backs whom up.
- [ ] **Shared-infra wiring** — Cluster Operator always; Finance Lead if money
      is spent; intelligence if information is consumed.
- [ ] **Recovery topology** — every long-lived node has exactly one recovery
      buddy; buddies form a ring; the CEO closes the loop.
- [ ] **Communication** — notify / send_message channels verified working;
      cross-node direct, no CEO relay.

## When a CTO is needed (and when not)

The CTO-as-manager layer is dissolved by default. The **only** criterion for
reintroducing it is **cross-dependency between infrastructure domains** (they
start entangling each other), not their count. Three independent infra domains
(ops / finance / memory) need no coordinator. Signals: PoCs repeatedly need to
coordinate with each other, or leads complain they must consult multiple infra
owners and reconcile the answers themselves.

## Template vs. instance — the separation rule

- **Template** = this skill (generic, reusable by anyone).
- **Instance** = `references/example-cluster.md` — one real cluster's
  instantiation of this template, **sanitized**: no user's private details,
  personal information, relationship or health details. Write it so a stranger
  could read it.
- **The user's private configuration** (their actual projects, preferences,
  health, relationships) **never enters this repo.** It lives in the cluster's
  own memory pool.

## Role spawn prompts

Concrete spawn-prompt templates for every role: `references/role-prompts.md`.
Read it when spawning or resurrecting a role agent; fill the placeholders
({USER_NAME}, {CLUSTER_NAME}, ...) and adjust to the cluster.

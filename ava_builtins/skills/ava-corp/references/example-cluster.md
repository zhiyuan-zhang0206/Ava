# Example cluster — a sanitized reference instance

This is one real cluster's instantiation of the `ava-corp` template, written
so a stranger could read it: all user-identifying details (name, email,
relationships, health) are removed, and agent ids are illustrative placeholders
(#101...). Use it as a worked example of how the template's roles and rules
map onto an actual fleet.

> The example is sanitized: it contains no private user data. The real cluster's
> role assignments and task lists live in the cluster's own memory pool (not in the repo).

## Organization

```
CEO #101
├── Corp division — project leads, each reporting directly to the CEO
│   ├── Flagship product lead      #102   (P0)
│   ├── Intelligence lead          #103   (project lead; internal fan-out
│   │                                      to a finder pool, weekly report)
│   ├── Content services           #104/#105  (dormant)
│   ├── Video generation           #106   (dormant)
│   └── Monetization experiments   #107/#108  (dormant)
├── Personal services — always-on, serve the user directly
│   ├── Butler                     #109
│   ├── Physical health lead       #110
│   ├── Mental health lead         #111
│   └── Philosopher                (planned; absorbs the User Proxy in
│                                   the light configuration)
└── Shared infra — CEO-owned; consulted, not reported to
    ├── Cluster Operator           #112   (only executor of rollouts;
    │                                      absorbed a former infra domain)
    ├── Finance Lead               #113   (reframed from "CFO")
    └── Memory Steward             #114
```

## How this instance mapped the template

- **Terminology pinned**: "POC" (ambiguous abbreviation) retired; roles are
  "lead" (owns a project) vs "PoC" (consulted, not reported to). Old labels
  carrying the retired term were renamed.
- **Reframe, don't kill**: the Finance role was reframed from "CFO" (manager
  flavor) to "Finance Lead" (advisory PoC) without terminating the agent; a
  former infra domain was folded into the Cluster Operator instead of keeping
  a parallel role.
- **Shared infra re-parented**: Cluster Operator and Memory Steward were moved
  from under the flagship project to report to the CEO directly — shared
  infrastructure must not hang under one project.
- **Dormant roster**: paused projects keep their roles on the roster;
  restarting them goes through `resurrect` (preserves context), never a fresh
  spawn.
- **Recovery ring**: every long-lived node has exactly one recovery buddy;
  buddies form a ring; the CEO closes the loop.
- **Direct communication**: cross-role alignment is direct `send_message` +
  shared memory; the CEO relays nothing.
- **Deployment discipline**: cluster rollouts are executed by the Cluster
  Operator alone; no agent runs `ava.self.update()` on its own.

## Role lifecycle in practice

| Event | What this cluster does |
|---|---|
| New project starts | Spawn one lead (clean spawn, `fork_from=None`), assign P0–P3 priority |
| Project pauses | Mark role **dormant** (agent terminated, role on roster) |
| Project restarts | `resurrect` the dormant lead — memory intact |
| Infra role changes hands | Outgoing agent spawns the successor; mutual handoff via files before stepping down |
| Load grows | Cluster Operator proposes a dedicated Resource Monitor instead of overloading itself |
| User presence grows | User Proxy may split out of the Philosopher into its own agent |

## Division rules recap

| | Project roles | Personal services |
|---|---|---|
| Trigger | CEO assignment / roadmap | Life events (calendar, health data, conversation) |
| Cadence | P0–P3 priorities, pausable | always-on, never off, low interruption |
| Deliverable | Ships outward / product value | Serves the user directly, no outward delivery |
| Reports to | CEO | The user directly |

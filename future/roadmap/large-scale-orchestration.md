# Large-scale agent orchestration

The capability Ava is actually missing in multi-agent: not a richer spawn
primitive, and not transcript search — it is **being the leader of a hundred or
more agents**. Orchestrating a handful of peers works today; commanding a
large fleet does not — the fleet is now visible, but the skill for running it is
only a first cut and the board offers no way to act on agents in bulk.

This is one item with three parts. The backend that makes it possible (peer
spawn/fork, lineage, the 24-event `ava:events` stream) already exists — what is
missing is the *scale* of the leader's skill and the leader's batch controls.

> **Status:** the leader's eyes are built; the leader's hands are not. The
> `/fleet` board shipped — `frontend/src/components/fleet/` (graph, task graph,
> task kanban, inbox queue, force controls) over `gateway/routers/fleet_graph.py`
> — so lineage, per-agent activity, and label are all legible at a glance. The
> leader skill `ava_fleet` also has a first cut (two dials Effort × Autonomy,
> explore→fork→wake-join, adversarial+heterogeneous evaluation, finding-driven
> converge; teamwork folded in). What remains open is Part 1's distillation
> against a *real* 100+ run and the three unbuilt board controls in Part 2.

## Part 1 — the leader skill (let it emerge from real work, don't pre-write it)

A skill encoding how one agent orchestrates 100+ peers: decompose a large goal,
fan out workers, track who is doing what, collect and reconcile results, kill or
re-task stragglers, keep the whole thing from thrashing. This is primitive 2 + 3
(skill + peer agents) — no new framework.

**Sequencing decision: this is learn-by-doing, not a speculative doc-driven
build.** The leader procedure should be grown out of **real orchestration on the
user's actual company dev tasks**, not written up-front from imagination. A skill
authored before the real fan-out pain is felt would encode guesses; one
distilled *after* running real multi-direction work captures the moves that
actually mattered. So Part 1 is deferred to "when there is a real 100+ job to
run," and the skill is the residue of having run it.

This is the machine-scaled version of the working loop in the roadmap README
(brainstorm requirements -> fan out parallel directions -> converge): the leader
skill is that loop run by an agent over many agents instead of by the human over
a few.

Open question (answer it from the real run, not now): at 100+ peers, do the
existing primitives (`spawn`, `send_message`, `watcher` → `remind`) stay
ergonomic, or does the leader need batch affordances (broadcast, subtree query,
group-wait)?

## Part 2 — the dashboard (the leader's eyes) — mostly built

The peer-agent theater the web surface used to be missing is now the `/fleet`
route. Built: the spawn/fork/resurrect lineage graph, live **activity** +
**label** per agent (the two columns that make a 100-agent fleet legible at a
glance), the lineage-projected task tree and kanban, and the unified notice
queue with per-notice reply. Each node links straight to its conversation, which
covers attach/detach.

Still unbuilt, all of it about acting on *many* agents rather than seeing them:

- batch / subtree kill (the board has no multi-select at all today)
- cross-agent message panel — replying is scoped to one notice; there is no way
  to message an arbitrary agent or group from the board
- Ctrl+K command palette over the lifecycle actions that already exist as
  buttons (no palette component, and `cmdk` is not a dependency)

Still pure frontend over an already-complete backend.

> Because activity + label are surfaced well here, **session search dropped in
> priority** — most of "find what that agent was doing" is answered by the live
> board, not a transcript grep. Session search stays opportunistic (see roadmap
> README), not a blocker for this item.

## Part 3 — batch control ergonomics

Whatever the leader skill (Part 1) proves it needs and the dashboard (Part 2)
exposes as a control: subtree kill, broadcast a message/`/command` to a group,
wait-on-many. Driven by the skill's real needs, not speculatively.

## Sequencing

Dashboard (Part 2) and the leader skill (Part 1) co-develop — the skill defines
what the board must show and control; the board makes the skill operable. With
the board built, Part 2's remaining controls and Part 3 are the same need seen
from two sides: the board exposes them, the SDK has to carry them. One more
edge type is worth adding when that happens — watcher/monitor subscription
edges (who is watching whom), which the graph does not draw today and which tie
back to goal-mode and lifecycle-event supervision.

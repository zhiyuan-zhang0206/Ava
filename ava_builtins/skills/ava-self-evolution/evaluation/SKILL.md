---
name: evaluation
description: "Why evaluation is hard — the discipline behind the self-evolution evaluation loop: real traces carry no ground truth, eval cases are first-class citizens (strong / diverse / representative), and the trace audit is anti-cheat by structure (cluster memory / web search / completed-task results). Read when running or designing an evaluation."
---

# Why evaluation is hard

This is the discipline section of the
[`ava-self-evolution`](../SKILL.md) skill. Evaluating a skill is not a
test-suite problem — it is a measurement problem over a real, uncontrolled
distribution. This document states why, and the standard the parent skill's
Evaluation Loop runs against (`reference/evaluate.py`, spawning fresh
agents).

## Real distribution, naturally scarce representativeness

Eval cases are built from the user's real run traces — the dataset. Real
usage is exactly what makes them valuable, and exactly what makes them hard:

- **Real data, no ground truth.** A real trace carries no answer key. The
  labeler (`reference/label.py`) only detects visible breakage (breach,
  corrections, re-prompts, exec failures) and the rubric scores proxy
  signals (completion, efficiency), not correctness. Nothing in a trace says
  "this was the right output".
- **Representative cases are naturally scarce.** Real usage is long-tailed:
  most runs exercise a few common patterns; the interesting ones — the
  failed/fumbled runs worth re-running — are rare. Scarcity is structural,
  not a collection bug. Treat it as a budget on how many measurements the
  loop can afford, not a problem to engineer away.
- **Measurement is comparative.** With no absolute truth, a score means
  something only as a delta: the same case under the old skill vs the edited
  skill. That is why the loop re-runs the same case set, not fresh tasks
  every round.

## Eval cases are first-class citizens

A case is a durable, reviewed asset with the same standing as the dataset
itself — not a side input picked ad hoc when a run is needed. A case is a
dataset record selected for re-running through `evaluate.launch`; case setup is
the step where the loop's validity is decided, and it is deliberate.

Acceptance standard — a case must be **strong**, **diverse**,
**representative**:

- **Strong.** The task's outcome must hinge on the skill under test: success
  depends on reading and following the skill text, and the skill's recent
  change plausibly affects the result. A case the skill cannot influence
  measures nothing.
- **Diverse.** The case set covers the skill's distinct behavior areas —
  different instruction sections, tool paths, failure modes — instead of
  clustering on one pattern. N near-identical cases measure one thing N
  times.
- **Representative.** The case is drawn from a real trace — prompt,
  environment, and expected behavior come from what actually happened, never
  invented — and it is replay-safe (pure read/compute, the `is_replay_safe`
  gate), because the loop re-runs it for real.

Acceptance is a review step, not an assumption: before a baseline runs, each
case is checked against the three criteria and the set against coverage. A
case that fails is dropped or re-derived from another trace — losing a case
is cheaper than trusting a measurement built on a weak one.

## Execution: spawn a batch of agents

The loop runs a case set by spawning one fresh agent per case
(`evaluate.launch`, `ava.agents.spawn`), so each agent starts from the
current skill text with no memory of prior runs. Execution is asynchronous by
design: spawn the whole batch up front, let the agents run, then gather
(`evaluate.poll` → `evaluate.gather`). Each agent receives its case's task prompt and nothing else — no hints, no
context from the original run.

## Trace audit: fine-grained, anti-cheat by structure

The audit reads the resulting trace, not just the score: what the agent
actually did — tools called, files read, messages sent — is checked against
what the task intended. A fine-grained audit is the only defense against a
score that looks right for the wrong reason.

The audit structure is layered; each layer has one responsibility and one
leak boundary. The goal is not a complete proof of no-cheating (not
achievable) but a **clear structure**: every layer knows what it isolates,
and any layer not yet enforced is a named, known gap rather than an
unexamined assumption.

Three leak surfaces are the standing audit checklist — each a way a
case-running agent could see the answer instead of doing the work:

1. **Cluster memory.** Spawned eval agents share the cluster: the shared
   memory pool (`ava.memory`) can hold notes about the very runs being
   evaluated (deep-dives, proposals, reports). An agent that finds "run #X
   failed because of Y" has the case answered. Boundary: eval agents cannot
   read evaluation-relevant memory — either they run isolated from the pool,
   or the pool is swept for eval-relevant content before a case set runs.
2. **Web search.** The task prompt may be searchable; an agent with
   `ava.web` can look up the answer instead of doing the work. Boundary:
   eval agents' outbound network is restricted to what the task requires.
3. **Completed-task results.** The original run's transcript and final
   output live in the dataset (`$AVA_HOME/self_evolution/`), in DB
   checkpoints, and in the source agent's workspace; `ava.agents.get_last_message`
   on the source agent exposes its final output. An eval agent that reaches
   any of these can copy the answer. Boundary: eval agents cannot read the
   original run's artifacts.

Layer map — responsibility, leak boundary, and current status:

| Layer | Responsibility | Leak boundary | Status |
|---|---|---|---|
| Task selection | Only side-effect-free tasks get re-run | `is_replay_safe` gate (tool-call allowlist) | Implemented |
| Data isolation | Eval run never touches production data | **Open** — `evaluate.launch` spawns ordinary cluster agents | Planned |
| Side-effect containment | Agent's file/OS effects stay contained | Container mode (effects die with the container) | Implemented in harness; not used by `evaluate.launch` |
| Memory isolation | Eval agent cannot read eval-relevant pool notes | Empty per-agent `ava.memory` pool; shared index and passive recall suppressed | Implemented at the SDK layer; raw filesystem and DB reads need next-phase OS sandboxing |
| Network restriction | Eval agent cannot web-search the answer or drive connected clients | `ava.web` / `ava.understand` removed unless explicitly allowlisted; `ava.mcps` / `ava.ui` always removed | Implemented at the SDK layer; raw outbound network needs next-phase container mode |
| Result separation | Eval agent cannot read original-run artifacts | SDK hides task and last-message reads; gateway rejects isolated last-message callers | Implemented at the SDK + gateway layers; raw filesystem and DB reads need next-phase OS sandboxing |
| Trace audit | Score is verified against what the agent actually did | Read of `tools_called`, file reads, messages per run | Partially — signals in rubric/label; manual deep-dive reads traces |

Rule of thumb: **anything an eval agent can read that a fresh user could not
read before attempting the task is a leak.** The per-run audit is a pass over
the agent's tool calls and touched files looking for the three leak surfaces;
a run that touched one is invalidated (or its score read with the caveat),
and the leak is fixed at its layer — never by blaming the agent.

Audit pitfall from practice: the weekly dataset's `transcript` field
truncates after compaction, so a fine-grained audit cross-checks the daily
datasets and the run's own workspace instead of trusting the transcript alone
(see the gmail deep-dive note, 2026-08-10).

## Status of this document

The structure above is the standard; enforcement is deliberately staged. The
SDK and gateway boundaries are live today; OS-level filesystem, database, and
raw-network containment remains the named container-mode next phase. Until
then, `caller` on the result endpoint is client-reported and can be spoofed
under the accepted peer-trust model, and an isolated agent can ask a
non-isolated helper to read results; container mode is the mitigation for both.

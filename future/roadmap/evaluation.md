# Evaluation — the hard prerequisite

A big, hard, **separate** topic. It earns a roadmap entry for two reasons: it
gates the north star (autonomous self-code-evolution cannot run without an
objective scorecard — [`self-code-evolution.md`](self-code-evolution.md)), and
it is itself a major build, not a checkbox.

## What exists today

SWE-bench is wired — the coding line has a scorecard. The harness lives in
the private MyAva repo's `benchmarks/` + `evals/` (moved 2026-08-12; conventions in
[`.agents/skills/run-swe-bench/SKILL.md`](../../.agents/skills/run-swe-bench/SKILL.md));the containerized substrate that runs it is [`docker-sandbox.md`](docker-sandbox.md).
That is the solved part.

## The hard part: task sourcing beyond SWE-bench

The unsolved, high-value problem is **where the tasks come from**:

- **Coding tasks** — SWE-bench gives a stream; other coding-task *types* are an
  open sourcing problem.
- **Life-helper tasks have no benchmark at all.** Real tasks (digest these
  people, book this appointment, buy this thing) come from the user's own daily
  life — 10-20 genuine pain points a day.

## Task capture = a task-intake skill (simpler than it first looks)

The capture mechanism is not a harvesting machine — it is **a skill** (general,
read by Claude Code *and* the Ava agent alike, not repo-specific). When the user
brings a raw task, the skill makes the agent:

1. **brainstorm with the user first** — align on what the task actually is
   (build on the existing superpowers `brainstorming` skill, don't start from
   scratch);
2. **search for existing solutions** and surface a few candidate paths;
3. **think divergently about scope** — should this task be widened or narrowed;
4. **pin a success criterion** — a checkable outcome / end-state.

Step 4 is the load-bearing addition for *eval*: it is what turns a well-scoped
task into an **eval-ready** one. Without it we accumulate tasks that are
understood but not gradeable. With it, every captured task arrives with its own
"how we'll know it was done well," which is exactly the rubric the autonomous
loop grades against.

## Real evaluation is coupled to Docker isolation

Real autonomous eval — run a task, grade the outcome, repeatably — needs the
exec sandbox ([`docker-sandbox.md`](docker-sandbox.md)). You cannot safely run a
"buy things" or "modify your own code" task on the bare host, and you cannot get
a clean repeatable grade without isolation. So **real evaluation and Docker
isolation are one coupled topic**, not two independent items: they land
together, and together they unblock self-code-evolution.

## Why it matters (the money thesis)

The eval is the measurement substrate for the entire ambition. The application
north star — a system that autonomously runs an online business — only converges
if there is an objective scorecard for "did the agent actually do the task well."
No scorecard, and the autonomous loop optimizes nothing and silently regresses.

## Open question

With the intake skill carrying the capture + success-criterion, the remaining
piece is lighter: collecting the resulting task specs into a **standing,
replayable** eval set (store the spec + its criterion, replay it later in a
clean containerized cluster). That is plumbing on top of the skill, not an
unsolved research problem — the hard judgement (what the task is, how it's
graded) has been pushed into the conversation where the human is anyway.

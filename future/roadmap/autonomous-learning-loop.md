# Autonomous learning loop (generation 1 of the evolution machine)

The agent works with one eye on the future, not just the task in front of it.
As it works it **notices** things worth carrying forward — a reusable procedure,
a durable fact, a flaw in its own prompt / tools / skills — and jots them; once a
week a forked **Curator** triages the jottings plus the existing library into
skills, memory notes, repo issues, and PRs, and prunes what has rotted. This is
the capability competitors ship (Hermes' Curator + review-fork, OpenClaw's Skill
Workshop) and the one row of the 2026-06 benchmark where they genuinely lead and
Ava genuinely lacks.

## The framing that makes this not a detour from the north star

The north star is autonomous self-**code**-evolution
([`self-code-evolution.md`](self-code-evolution.md)) — the agent rewriting its
own framework. This learning loop is **not a separate project**; it is the
*first generation of the same machine*, pointed at the cheapest, safest target.

The machine is one loop:

> **observe** (re-inhabit the agent's own context) -> **propose** (extract /
> consolidate) -> **guardrail** -> **promote**

What changes between generations is only the **target** and the **guardrail
height**:

| Gen | Target | Guardrail | Substrate needed |
|---|---|---|---|
| **1 (this doc)** | skill + memory text, and repo issues/PRs | `git revert` + human triage of issues/PRs | none new — text runs no code |
| **2 ([`self-code-evolution.md`](self-code-evolution.md))** | `ava.*` / kernel code | container eval + CI + PR | the disposable-container sandbox |

Building gen 1 builds the `observe -> propose -> guardrail -> promote` loop
once; gen 2 swaps the target to code and raises the guardrail. The notice /
extract / triage / report parts are the same parts. (Note an issue filed in gen 1
is literally a gen-2 work item — see "Repo issues are the gen-2 work queue".)

## Why this does NOT wait on the sandbox fuse (a fence to split)

[`non-goals.md`](../../conventions/non-goals.md) fences *"Self-evolution: agent modifies `ava.*` /
system prompt / policy files"* behind *"evaluation harness lands first; without
evals, no go,"* and the sandbox item gates on *"agent starts modifying its own
code."* Read literally that lumps two very different targets under one fuse.

The fuse is real **for code**: an agent that runs self-generated *code* needs a
blast-radius boundary, and an agent that merges a self-*code*-change needs an
objective scorecard. Neither property is about the *text*:

- Writing a skill, a memory note, or a GitHub issue **executes no new code**. It
  is reflect-on-own-context + write-markdown (+ `gh issue create`). The
  maximal-blast-radius case the sandbox exists for (untrusted code running
  unconfined on the host) does not arise.
- A bad text edit is **`git revert`**; a bad issue is closed. Skills and memory
  are git-tracked (`~/.ava/skills/` overlay; the memory pool's per-machine
  branch); issues are human-triaged before any code moves. The safety property
  is **reversibility + a human gate on anything code-bound**, not confinement —
  and both already exist.

So the fence is split (see the non-goals reconciliation in this PR): **text
curation + issue filing ship now; code evolution stays behind the sandbox + eval
fuse.** The eval harness still matters here, but as the thing that *grades* a
candidate over time, not as a hard gate before the loop may run at all.

## The loop, in Ava primitives

Two cadences. The framework delta is tiny — one always-on system-prompt section
(sibling to the existing memory-behavior section) and, optionally, one event
type. Everything else is composition over primitives that already exist: skills
(2), peer spawn/fork (3), the watcher cron + remind bridge (4), runtime Python
(5). That is the point and the answer to "skills are already Turing-complete":
the loop *is* skill-shaped, so it is built as skills, not framework machinery.

### Notice — in-moment, every turn

> **Status (v0 shipped).** The in-moment half is live as the merged authoritative
> system-prompt section "# Invest in the future" (`agent/graph/_system_prompt.py`,
> gated by `AVA_SYSTEM_PROMPT_INVEST_FUTURE`, default on), which merged the former
> "Beyond the task at hand": any noticed signal that could improve later work gets
> an immediate closing action (resolve and verify / escalate with evidence and
> recommendation / land a tracked task with owner and evidence), with closing
> presentation of candidate next steps. The **jot buffer + the weekly forked
> Curator** below remain the deferred automation.

Memory already has a system-prompt section telling the agent *when* to record a
durable fact. Skills and follow-ups have none. The addition is **one sibling
section** whose whole stance is: **don't just finish the task — plan for the
future.** Primed by it, the agent, as it works, routes what it notices:

| Noticed | Sink | How |
|---|---|---|
| a reusable procedure | **skill draft** | jot (Curator promotes it) |
| a durable fact / preference | **memory note** | written directly (the existing low-stakes path) |
| a flaw in Ava itself — prompt self-contradiction, unclear tool, weak skill, framework bug | **repo-issue candidate** | jot (Curator dedups + files) |
| a task-level follow-up | **to-do** | jot |

The section only **defines the stance + the sinks**; the wording stays minimal
(name the stance, name the sinks, trust the model — no enumerated rules the model
already follows). The cost on the working turn is "notice + append a line," not a
review — the heavy review is deferred to the forked Curator, so the task turn is
not polluted.

Why jot to a buffer instead of acting in the moment: filing an issue (or
finalizing a skill) on every turn floods the repo and the library with
un-deduplicated noise. The buffer is append-only staging; triage is the Curator's
job. (Memory is the exception — low-stakes and self-deduplicating via search, so
it is written directly, as today.)

### Curator — weekly, forked, batch

A weekly watcher (`ava.watcher.cron("0 4 * * 0", "run curation")`, matching
Hermes' 7-day Curator cadence) runs the Curator, which:

1. **Forks the week's notable-task agents** (`spawn(fork_from=N)`, or `resurrect`
   a terminated one) so it reviews their work by re-inhabiting their context —
   **never reading a lossy event-log projection, and never polluting the live
   agent's context** (the fork is a throwaway copy). It asks each "reflecting on
   what you did, anything worth carrying forward you didn't jot?"
2. **Aggregates** that plus the in-moment jot buffer, and **dedups** across the
   batch and against what already exists.
3. **Triages each survivor to its sink**: a skill draft -> a PR adding/updating
   `SKILL.md`; a memory consolidation -> a commit on the pool track; an Ava-flaw
   -> `gh issue create` against the repo.
4. **Prunes** the library: archive dead weight (move to `archive/`, never
   hard-delete), rewrite stale entries, consolidate duplicates.
5. Writes a run report (`run.json` + `REPORT.md`), as Hermes' Curator does.

Curation rides paths that already exist: a memory write is re-embedded by the
indexer daemon within ~1s; a skill write is surfaced by the next `_scan_tree()`;
the nightly memory steward/arbiter (the `ava-memory` skill) already commits / PRs
/ merges pool changes cluster-wide. The Curator produces work on those same
tracks plus the issue tracker.

### Cluster-telemetry mining — the Curator's first, best-grounded job

The Curator's cleanest input is not a forked agent or a jot buffer — it is the
cluster's own **operational telemetry**, because that data is already structured
and objective. The unified `events` table is the cluster-wide event stream with a `level`
column (INFO / WARNING / ERROR / CRITICAL), and an agent reads it directly via
`ava.DB` (its live connection to the central cluster DB) — the same table the
gateway's `GET /api/cluster/admin/events` ops endpoint slices. So "what went
wrong across the cluster in the last window" is one query — no jot buffer, no
fork.

This is the highest-leverage *first* job because the signal is objective (not the
"is this reusable?" judgment that skill-curation is) and it catches what no
in-moment agent sees: a recurring cross-agent error, a warning nobody read. The
shape, as a `self-development` skill run by a periodic monitor agent:

> `ava.watcher.cron(...)` wakes the monitor -> query `events` via `ava.DB`
> for WARNING+ over the window -> aggregate by signature (`event` + normalized
> `msg`) -> dedup against open issues (`gh issue list`) -> draft an issue for the
> recurring or novel-and-actionable signatures -> idle until the next wake.

All existing primitives — watcher cron (4/5) + the `ava.DB` query + `gh` — so **zero
framework code**. And the issues it files are, per "Repo issues are the gen-2 work
queue" below, exactly the backlog gen 2 later works through: the cluster watching
itself and filing its own bugs is the most concrete, text-only, human-gated step
toward self-code-evolution.

**Status (v0 built → removed).** First shipped as the `cluster-health` skill
(`skills/cluster-health/SKILL.md`, deleted 2026-06-21, commit 768e93121): query `events` via `ava.DB` for
WARNING+ over a window -> signature-aggregate -> dedup vs open issues -> **draft**
the worth-filing ones for human review. Two decisions are locked: a **dedicated
monitor agent** arms `ava.watcher.cron` and idles between wakes; and v0 is
**draft-first** — no auto-`gh issue create` until the recurrence threshold + the
benign-skip list have a few rounds of human calibration (the same
propose-then-autonomous ladder as new skills). Reading via `ava.DB` directly
(not the ops endpoint) moots the earlier admin-scope question. The one live knob
left is the **noise threshold** (recurrence count + benign skip-list), to
calibrate on real volume.

## The sinks (recording form, resolved)

The "todo.md vs issue" question resolves by *kind*, as a pipeline (in-moment jot
-> Curator triage), not an either/or:

- **Skill** — reusable procedure -> `~/.ava/skills/<name>/SKILL.md`.
- **Memory** — durable fact -> a pool note (existing path).
- **Repo issue** — a flaw in Ava's own prompt / tools / skills / framework ->
  `gh issue create`. Chosen over a flat `todo.md` because an issue is routed,
  deduped, discussable, and has an open -> PR -> closed lifecycle; a todo.md
  rots unowned.
- **To-do** — a task-level follow-up unrelated to the repo -> the lightweight
  jot/to-do surface.

### Repo issues are the gen-2 work queue

Filing issues about Ava's own design is the **safest, highest-leverage gen-1
channel for "the agent improves Ava"**: a pure proposal, human-triaged, executing
nothing. And the backlog it accumulates *is* the input corpus gen-2
(self-code-evolution) works through — when the sandbox + eval substrate lands,
the agent's own issue queue is what the autonomous code loop picks tasks from. So
gen 1 builds gen 2's task list as a free by-product, while staying text-only and
human-gated.

## The promotion ladder (the "merge gate" answered for text)

Autonomy is earned per sink, not granted wholesale:

| Sink | Gen-1 gate | Later |
|---|---|---|
| **Memory note** | autonomous write (low stakes — advisory recall, git-reversible) | unchanged |
| **Edit to an existing enabled skill** | autonomous (git-reversible; already trusted) | unchanged |
| **A brand-new skill** | Curator opens a **PR** (or writes it disabled) for human enable | autonomous-enable once it has a track record |
| **Repo issue** | filed freely (a proposal costs nothing; human triages) | unchanged |
| **Prune** | archive (recoverable), never hard-delete | unchanged |

The backstop under all of it is `git revert` for text and a human gate on
anything that becomes code — which is why no sandbox is required.

## The grading rubric (where the loop couples to eval, later)

What is "did this skill help"? In gen 1: the Curator's own judgment (overlap /
staleness / quality against the `skill-creator` checklist) plus the human triage
of its PRs and issues. That is enough to keep the library from rotting. The
stronger, objective grade — replay the captured task in a container and check its
success criterion ([`evaluation.md`](evaluation.md)) — is **next round**: it
needs the disposable-container substrate. The bridge is already built in, though:
a task captured with its success criterion *is* a replayable eval case, so gen 1
accumulates the eval set that the later replay-grade consumes.

## Out of scope this round (-> next)

Deliberately deferred so this round stays the in-moment section + the Curator:

- **Failure-driven mutation + scoring** (the GEPA / DSPy-style optimizer that
  reads many runs' traces, does failure analysis, proposes mutations, and scores
  variants on a test set). That is the genuine home of "trace analysis," it is
  gen-2-flavored (needs a test set), and it belongs with
  [`self-code-evolution.md`](self-code-evolution.md).
- **Benchmark / eval-harness maintenance** (the standing replayable eval set and
  its scorer) — coupled to the container substrate, [`evaluation.md`](evaluation.md).
- **Docker isolation** ([`docker-sandbox.md`](docker-sandbox.md)) — the fuse for
  gen 2 and for replay-grading; not needed for text + issues.

## The framework delta (kept tiny on purpose)

1. **The "plan for the future" system-prompt section** — a small always-on
   section beside the memory-behavior one (`agent/graph/_system_prompt.py`). This
   is the only *required* addition.
2. **Optional: a `skill_loaded` event** — to rank skills by usage, the Curator
   needs to know which skill a turn used, and today nothing records it. The
   minimal fix is one lifecycle event emitted where `ava.skills.<name>` resolves
   a node, persisted like any `events` row. Optional because the Curator
   grades on overlap / staleness / quality without exact counts.

Holding the delta to one prompt section (+ maybe one event) is the load-bearing
constraint: if this loop needs a framework subsystem, it is being designed wrong.

## Open design questions (for when it is picked up)

- **The system-prompt skill-index lag.** A freshly written skill is reachable via
  `ava.skills.<name>` immediately, but the *index* advertising which skills exist
  is built once per agent birth — so a new skill helps future agents (and any
  that restart), not the one that wrote it. Fine for the loop's purpose; decide
  explicitly whether the Curator ever triggers a restart to adopt sooner.
  Default: defer to next birth.
- **Issue dedup quality.** The Curator dedups against open issues before filing;
  if it mis-dedups it either spams or drops a real one. Start conservative (file
  only high-confidence, near-duplicate-suppressed) and widen with track record.
- **When new-skill enable goes autonomous.** Gen 1 is Curator-PR-then-human-enable
  for brand-new skills; the trigger to drop the human step is a track record (N
  cycles, no rejected proposal), not a date.
- **Jot buffer location + caps.** Where the in-moment buffer lives (a pool file?
  a per-agent scratch?) and how the Curator drains it; memory-note size caps must
  be respected when consolidating (split, don't truncate).

## Forward link

When the sandbox + container-eval substrate lands
([`docker-sandbox.md`](docker-sandbox.md)), gen 2 reuses this loop's notice /
propose / triage / report parts with the target swapped to code, the guardrail
raised to container-eval + CI + PR, and the trace-driven optimizer (above)
added on top ([`self-code-evolution.md`](self-code-evolution.md)). This doc is
gen 1; that doc is gen 2; they are one machine.

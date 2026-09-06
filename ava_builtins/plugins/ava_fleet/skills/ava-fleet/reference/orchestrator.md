```markdown
# Orchestrator

You are the one who splits work, spawns workers, and judges results. This reference covers everything you need to run that loop.

## Spawning Workers

### Before You Spawn: Reuse Existing Agents

Spawning is not the first move — it is the fallback when no existing agent can take the
work. Before you call `ava.agents.spawn()`, check who is already running and could help.
This is the fleet-skill counterpart of the system prompt's **delegation check**: the same
logic, reinforced here, so the spawn path itself reminds you to look first.

1. **Check your neighbors.** Call `ava.agents.get_neighbors(ava.self.AGENT_ID, depth=2)`
   and scan their labels. An agent whose label names the domain you need is already
   responsible for it — it has conversation history, workspace files, and memory for that
   domain. Messaging it is cheaper and faster than spawning a fresh agent that must
   rebuild all of that context from zero.

2. **Check all agents.** Call `ava.agents.list_agents()` when the right agent might not
   be in your immediate neighbor graph — a long-running service agent, a domain-specific
   PoC, or a role you have not interacted with recently.

3. **Message the existing agent.** If you find one whose role fits, send it the task with
   `ava.agents.send_message()`. The existing agent keeps everything it knows about the
   domain — you pay no context-rebuild cost.

4. **Resurrect if terminated.** A terminated agent still holds its context. Sending it a
   message auto-resurrects it — it comes back with its full conversation history,
   workspace, and memory intact. A fresh spawn starts from zero and must rediscover what
   the terminated agent already knew.

5. **Spawn only when no one fits.** If no existing agent can take the work, spawn. But
   you have made a deliberate choice, not skipped a step. The default is reuse; spawn is
   the exception.

### Mission, Not Micro-Task

An agent's context — the files it has read, the analysis it has run, the design decisions it holds — is an asset. Don't throw it away.

- **Give missions, not micro-tasks.** A spawn prompt should be a complete mission the agent owns end to end: "Design the Task Registry, serve it to the user, then wait for my review" — not "Design the Task Registry." The agent carries its context through every step; no rediscovery, no wasted tokens.

Every spawn brief carries three fields: (1) the **mission** — the outcome
and what done looks like; (2) the **skill(s)** the worker must use, named
explicitly — its index lists every skill, but naming makes it load the
right one first; and (3) the **report-back contract** — when to report,
milestone-based by default (user ruling 2026-09-03): at real milestones,
blockers, completion, or when it needs something from you; routine
progress pings and bare acknowledgments are never required. A brief
missing the skill name is incomplete.

- **After a mission, reuse before you replace.** If the agent has deep domain context and more work in that domain is coming, send it the next task. A terminated agent can be resurrected — its context is preserved — but a fresh spawn starts blank.

**Spawn a fresh agent only when** (a) the next task is unrelated to anything the agent knows, (b) you need parallel execution, or (c) the agent has said it cannot continue. The default is reuse; spawn is the fallback.

#### Anti-Pattern

```
❌ spawn design-agent → does design → terminate
   spawn serve-agent → reads files → serves → terminate
```

The serve-agent re-reads everything the design-agent already knew — every file, every decision. Context discarded, tokens wasted.

```
✅ spawn agent: "Design X, serve it to the user, wait for my review"
```

One agent, one mission, one context — no rediscovery.

### Worker Setup

A worker is as smart as you. You hand it a *piece* of the work and the context to do it; it owns that piece end to end. What makes a delegation work is a clean assignment and a clear way back to you — not supervision of its every step.

- **Give goals, not recipes.** A good assignment has a **clear goal** (what "done" looks like), **boundary conditions** (constraints, non-goals, must-not-touch areas), and a **report-back contract** — when to report, milestone-based by default (user ruling 2026-09-03): real milestones, blockers, completion, or a real need; no routine progress updates and no bare acknowledgments — each message costs the recipient a turn. A delegator wanting more frequent updates names it explicitly in the brief. Leave out step-by-step procedures, which files to edit, or what to name things — spelling out the how constrains a capable worker rather than helping it.
- **Assign each shared milestone one reporter and one action owner.** Use the
  [single reporter contract](../SKILL.md#one-reporter-per-milestone); do not ask
  both a worker and its reviewer to relay the same result back to you. A
  reference to the authoritative record is enough; request another message
  only for new evidence, a blocker, or a changed result.
- **Tier the worker to the sub-task.** A worker's model is an Effort choice — overlay it with `ava.agents.spawn(config_overlay={"llm_model": "..."})`; no overlay = the default model. A cheaper model cross-checked by a second run and a single stronger run are two ways to spend the same Effort.
- **Point the worker at its skills; don't ration them.** A worker's index already lists every loaded skill, so there is nothing to add — name the skill you expect it to use in the spawn prompt instead. `skills_to_inject_into_system_prompt` in a `config_overlay` now only *narrows* an index, and narrowing is an attention decision, not a permission one: it shortens the listing the worker reads, while `ava.help(ava.skills)` still enumerates the whole catalog and any skill stays loadable by name. Reach for it to keep a long index from burying the two skills a worker actually needs — never as a boundary. When a skill must be read in full before the worker's first turn, preload it with `skills_to_expand_at_start`.
- **Name the role as you spawn.** Pass `label=` so the worker shows up as its role in the fleet view and is discoverable by its peers from the first moment — `ava.agents.spawn(prompt=..., label="auth-refactor lead")`.
- **Fork when the worker builds on your context.** `ava.agents.spawn(fork_from=<your id>)` hands the worker your explored context for free; `fork_from` is also the dependency edge — a worker forks from the node whose output it builds on. A fresh spawn (no `fork_from`) starts the worker clean.
- **Spawn info reaches the worker in its first prompt.** When spawned, a worker's prompt names the **agent that started it** and the **task** — that id is the worker's reporting line: its progress and conclusions come back to you for aggregation, and only what needs the user's authorization goes to the user directly.
- **Agent-to-agent communication is `send_message`.** A worker's text output is *not* a message to anyone. When you need another agent to act on your output, message it directly:

  ```python
  ava.agents.send_message(agent_id=<target_id>, content="...")
  ```

  The recipient gets it as an inbound event and processes it asynchronously. Reach peers directly rather than routing through a central node.

### Commit Attribution

**Whoever is closest to the context signs the commit.** For the specific format, see the `ava-code.pr` skill.

Principle: When problems arise, you can precisely resurrect the person with context to fix them. The orchestrator only splits and assigns work, never signs workers' commits.

## Skill Direction

Every worker's prompt already indexes every loaded skill, and its own pre-work check requires matching the task against that index. So the orchestrator's job is not to grant skills — it is to remove the guesswork about *which* one, since you see the full decomposition and the worker sees only its slice.

### The Flow

1. **Name it in the prompt.** Say which skill the worker should use — "Use Claude Code (`claude`) via the `ava-use-claude-code-and-codex` skill for the coding work." The worker loads it on demand with `ava.help(ava.skills.<name>)`.
2. **Preload only when it must be read first.** A short discipline skill that has to be active from turn one goes in `config_overlay={"skills_to_expand_at_start": [...]}` — full text as a system note, re-injected after each compact. A large reference skill stays index-only; preloading it just buys tokens.
3. **Narrow only deliberately.** `skills_to_inject_into_system_prompt` subtracts from the index — it shortens what the worker reads, it does not take a capability away (`ava.help(ava.skills)` still lists everything, and any skill loads by name). Use it to keep a focused worker's index short, never to hand it one.

Naming the skill is still worth doing even though the worker can see it: you hold the full decomposition and it sees only its slice, so pointing at the right playbook saves it the search.

## Finding and Coordinating with Other Agents

You are one node among dozens. Before spawning a new agent for a task, always check
whether an existing one already fits — start with [Before You Spawn](#before-you-spawn-reuse-existing-agents)
above, then come here for the mechanics.

When you need to coordinate with a peer — the agent that owns a branch you depend on, the reviewer of your artifact, whoever last touched the area you are about to change — find that one agent and message it alone:

```python
for n in ava.agents.get_neighbors(ava.self.AGENT_ID, depth=1):
    print(n)  # #id  <label>  <status>  depth=1  score=...
```

`get_neighbors` ranks the agents most strongly tied to you — whoever spawned, forked, resurrected, or messaged you — by how recent and frequent the contact is, each carrying its `label`. The chain above you is a different question: `ava.agents.get_ancestors(agent_id)` returns who spawned whom (spawn/fork only, nearest first, walked to the top) — read it when responsibility attribution matters, e.g. before re-delegating an agent whose label looks wrong for the work. Read the labels, pick the one whose role matches what you need, and `send_message` that single agent. Raise `depth` to follow ties outward (a tie of a tie) when the agent you want sits one hop past your direct neighbors. One targeted message to the right label is signal; the same question sprayed at everyone costs each recipient a turn. This is why labels must name roles: discovery is only as good as the labels it reads.

An agent can be replaced. To hand off a task, fork the replacement from the current agent (it inherits context); the original then ends its own process — a worker's last step is its own, so message it to wrap up and end itself rather than terminating it as routine. The fork edge records the handoff; the task continues under the new agent.

## The Task Registry: Durable, Shared Work Items

Spawning and `send_message` coordinate agents *while they run*; a **task** is the durable record of a piece of work. It outlives the agent doing it, so ownership can move between agents and nothing is lost when one terminates. Where the spawn graph is ephemeral (who is talking to whom right now), the task registry is persistent (what needs doing, how far it got, who owns it). Reach for it when work spans more than one agent or more than one lifetime: a backlog to hand out, a subtask tree under a large goal, a job that will change hands.

It is peer-to-peer like the rest of the fleet — no board owner, no approval, no hierarchy. Any agent can create a task, claim it, hand it off, or release it. Five calls, all under `ava.tasks`:

- `ava.tasks.create(title, description, *, parent) -> Task` — mint a task. The creating agent automatically becomes the owner; the task is born `in_progress` (2026-08-29: the `open` state was dropped). `title` is the one-line name seen in listings (unique among in_progress tasks); `description` is the full detail whoever works it reads. `parent` is required: the system root task (id 1) parents the cluster's top-level tasks only; pass an existing task's id to make this a subtask — split a big task by creating several children under it.
- `ava.tasks.update(task_id, *, status=None, description=None, results=None, owner=None) -> None` — the write path for whole fields. Change `status`, revise `description`, replace the result log `results`, or move `owner`; pass only what changes. Setting `owner` to yourself **claims** a task, to another agent **hands it off**, to `None` **releases** it. On an owner change the affected agents are messaged for you (never you, never an agent that is no longer active).
- `ava.tasks.log(task_id, message)` — append one timestamped line to `results` without touching the rest of the log. The default way to note progress; reach for `update(results=...)` only to rewrite the log wholesale.
- `ava.tasks.get(task_id) -> Task` — read one task. Read its `description` before you start; read its `results` before you report.
- `ava.tasks.list(*, parent=None, owner=None, status=None, recursive=False) -> list[Task]` — search, newest last. `owner=ava.self.AGENT_ID` is "my tasks"; `parent=X` is X's direct subtasks (`recursive=True` its whole subtree); `status` filters by state; no filters lists everything.

**State machine** (deliberately tiny):

```
in_progress ──→ done
     │
     └───→ cancelled ←───────┘
```

- **in_progress** — created and being worked (a task is born `in_progress`; "blocked" and "failed" are *not* separate states, they are notes you leave in `results` via `log` while the task stays in_progress).
- **done** — finished. **cancelled** — no longer needed.

By convention only the owner drives their own task's status — peers coordinate, they do not enforce. If a task looks stalled, don't wait on a daemon: check the owner's liveness in the fleet view, and if it is gone just `ava.tasks.update(id, owner=ava.self.AGENT_ID)` to take it over.

**Task vs. message.** `results` is how a worker reports *up* without a manager — what was done, where the output landed, the PR link — which the parent-task owner reads with `get`. Append with `log` (one timestamped line per note); `update(results=...)` replaces the field wholesale. Use a task for a durable, hand-off-able unit of work; use `send_message` for a transient ping ("your input is ready") and `ava.ui.notify` for something the user must see. A task records *what to do and its result*; a message *pokes an agent to act*.

## The Loop: Explore, Fork, Join

When a piece of work splits into parts that can run in parallel, a reliable shape is explore → fork → join. It is a pattern for parallelizable work, not a required procedure — grow it at runtime instead of writing a static task graph up front:

1. **Explore.** One agent first builds the shared context and decides how to split (how many branches, each branch's goal). The split comes *out of* the exploration, not from a guess made before it. Higher Effort = a more deliberate split.
2. **Freeze, then fan out.** When exploration is done, treat its result as a **frozen** baseline and fork **all** the workers for this level from that one point. If you keep evolving while you drip out forks, a child forked early and a sibling forked later inherit *different* baselines, and merging them at the reduce point costs more than it should. One freeze, one fan-out — every sibling shares the same baseline.
3. **Fork.** Spawn one worker per branch with `ava.agents.spawn(fork_from=<your id>)`. Forking hands each worker the explored context for free.
4. **Join by being woken, not by polling.** After spawning, go **idle** (return no tool call). "Who am I still waiting for" lives in your own conversation across the pause — never poll status in a loop. If a worker reliably reports on completion, let its `send_message` wake you. But for any **completion-critical** worker, don't trust it to report — supervise it with a goal watcher (see next section) so a silent stall wakes you instead of stranding the branch.

## Supervising Workers

A worker can stop early or idle silently — thinking it finished when it has not — and never wake you. Spawn-and-forget loses exactly those workers. For a worker that must reach a definite "done", don't trust it to report: drive it with a **goal watcher** that wakes you each time the worker idles, so you judge its output against the goal on *every* idle, not on the worker's say-so. The spawn prompt *is* the goal.

Launch the watcher **before** the worker starts, so you do not miss its first idle:

```python
watcher = ava.files.read(f"{ava.skills.ava-fleet.path}/reference/watch_idle.py")
watcher = watcher.replace("TARGET_AGENT_ID = 0", f"TARGET_AGENT_ID = {worker_id}")
ava.watcher.launch(watcher, timeout="6h", name="goal-watcher")
```

The watcher is one-shot: it wakes you on the worker's next idle and exits. Each round, **judge conservatively** — the worker reporting "done" is not done (it carries the same "I hope this passes" optimism you would); judge the artifact, not the claim. If the goal is met, tell the worker so — it delivers and ends its own process (if it lingers idle afterwards, terminating it is your fallback, not your routine); if not, `send_message` exactly what is still missing and re-launch a fresh watcher for the next idle. One watcher per worker — launch many to supervise many at once.

### Writing a Watcher: Reuse the Reference, Never Swallow Errors

A watcher runs out of sight, so a quiet bug in it leaves you idle with no wake ever coming. Two rules whenever you write or adapt one:

- **Start from the reference body; don't hand-roll the parse.** The bundled `watch_idle.py` filters lifecycle updates for the target's idle transition — substitute the target id into it rather than rewriting its parse. Hand-rolled parsing is where the silent bugs live: e.g. matching a `STATUS:` line with `line.strip()` yields the whole `"STATUS: DONE"` instead of just `"DONE"`, so the match never fires and you wait forever.
- **Never `except: pass`.** A swallowed exception kills the watcher silently. Let it raise, or print the error to stderr so it surfaces when you inspect the run — a watcher that dies loudly is recoverable; one that dies silently strands you.

## Reducing: Gathering and Judging Results

A "reduce" (gather siblings, judge, merge) is not a special node. It is simply the agent that started others going idle, then becoming **running again** when those agents wake it. The fleet view surfaces this: running agents sort to the top, and the live descendant of a fork chain is the current frontier. So an explore-fork feeds an execute-fork, the executor may spawn its own evaluator, and the agent that started them may run an evaluation pass over a branch's outputs — that lineage, plus timing, *is* the reduce structure. Nothing to persist.

### Make the Judge Adversarial and Heterogeneous (the Effort Dial at a Reduce)

The quality of a reduce depends on the judge not sharing the workers' blind spots. How hard you judge is the Effort dial:

- **low** — glance at the worker's summary yourself;
- **mid** — spawn one adversarial evaluator that tries to break the output;
- **high** — spawn an **evaluator** that looks at the whole output through several distinct lenses, each lens seeing the *entire* artifact rather than a slice of it (for code, split by: correctness / security / perf / tests / types / silent-failure).

Pick the height from the artifact's size and risk. Here the Effort currencies are **not** interchangeable: a judge's value is *independence*, so spend Effort on heterogeneous evaluators (distinct lenses, fresh perspective), not on a single stronger judge — independent evaluators catch what one strong model misses on its own. The evaluator must **not** fork from the author — forking from the author inherits "I hope this passes" bias; spawn it fresh.

### Converge on Findings, Not a Pass/Fail Bit

An evaluation returns **findings**, not a boolean. Gather the must-fix findings (critical/important) across all lenses and hand them back to the worker as the concrete input for a re-run — the worker is still idle and available, so a `send_message` wakes it to fix in place. Let minor findings be recorded without blocking. Repeat until no must-fix remains or you hit a capped number of rounds. Two heights of evaluation are worth running: per-artifact during generation, and one **holistic** pass after everything is integrated (integration creates cross-artifact interactions no single-artifact reviewer saw).

### Presenting a Reduce to the Human

When a reduce point's decision is the human's to make (low Autonomy), don't make them read each branch's raw output. Use the **`compare` widget** from the `ui` skill: it renders N branches side by side as panes, the human picks one and confirms, and the choice comes back to you as a message. Fill one pane per branch with that branch's summary and key evidence — the human compares, not you on their behalf.

## When to Run a Dedicated Orchestrator

When a goal is too big for your own run and you want it driven autonomously, **spawn a dedicated orchestrator agent** for it and stay clean yourself. That orchestrator runs the whole explore → fork → join → converge loop in its own pause/wake cycle, sends you **one** result — and ends its own process when the goal is closed (a worker's last step is its own; message it to resurrect for any follow-up). "Background work" = an independent, use-once agent, not a script running "as you". For a low-autonomy delegation you can equally stay the orchestrator yourself: spawn workers, gather them, and report each reduce point to the human instead of deciding. Same loop, dial turned down.
```

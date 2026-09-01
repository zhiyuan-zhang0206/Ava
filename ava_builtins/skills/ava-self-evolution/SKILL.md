---
name: ava-self-evolution
description: Mines real Ava runs for skill and plugin regressions and produces concrete quality fixes. Use for each ~100-run batch, the Monday weekly summary, or real-task quality trends.
---

# Self-Evolution

Ava improves by looking at its own real usage. Every ~100 accumulated runs
(daily traffic is ~100-270, so roughly daily), the batch flow builds a **trace
dataset**, finds the runs that went badly, ties them to recently changed
skills/plugins, and proposes concrete fixes. Monday is a summary pass over the
whole week. The dataset is durable — it grows batch by batch and is the
material you iterate skills and plugins against.

This is NOT a pre-merge gate, a benchmark, or a synthetic test suite. It reads
what actually happened.

## Backpropagation analogy

The evaluation loop optimizes skill text the way backpropagation optimizes
weights:

| ML concept | Self-evolution equivalent |
|-----------|--------------------------|
| Forward pass | Agent runs task with current skill |
| Loss | `rubric.py` scores (completion + efficiency) |
| **Backward pass (gradient)** | **Ask the agent directly: "what should change?"** |
| Weight update | Edit the skill text |

The backward pass is **80% agent self-reflection** (`evaluate.debrief()`)
and **20% trace-mining by a separate worker** — the agent that ran the task
knows best what tripped it up.

## The batch flow

```
collect dataset -> detect what changed -> mine bad runs + analyze -> (replay) -> report
```

Helper scripts live in `reference/` (hyphenated dir, not importable — run as
scripts with `.venv/bin/python`). Output lands under
`$AVA_HOME/self_evolution/` (private per deployment), not in the repo.

### 1. Collect the dataset

```
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/ava-self-evolution/reference/collect.py --days 1  # Batch run (~100 accumulated runs)
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/ava-self-evolution/reference/collect.py --days 7  # Monday weekly summary
```

The batch command reads the past day; the Monday summary reads the past week.
Both write one JSON record per run to `$AVA_HOME/self_evolution/dataset/<this-monday>.jsonl`.
Each record holds the task prompt, complete transcript (see Data source), tools called, objective
signals (turns, exec failures, compactions, delivery breach, user re-prompts,
user corrections, peer agent feedback), skills it touched, and a rule-based
`label` of **ok / fumbled / failed** (see `reference/label.py`). The script
prints the run counts.

**Correction signals** (two new data sources since 2026-07):

- `corrections` — user messages classified as redirection/criticism via
  keyword detection (\u4e0d\u5bf9, \u9519\u4e86, \u91cd\u65b0, wrong, incorrect, etc.); split from
  `followup_prompts` because a correction is a stronger failure signal than a
  neutral follow-up.
- `peer_feedback` — agent-to-agent messages (`source LIKE 'agent:%'`)
  classified as corrective feedback; another agent stepping in to correct is
  an objective signal that something went wrong.

Both feed `label.py`: any non-empty `corrections` or `peer_feedback` → fumbled at minimum.

Collecting is the point on its own — grow the dataset on every batch, even in a quiet one.

### 2. Detect what changed

Kernel-resident skills and plugins (L4 of the four-layer modification model —
`decisions/2026-08-19-four-layer-modification-model.md`):

```
git -C ~/.ava/source log --since='1 day ago' --name-only --pretty='%h %cI %s' -- ava_builtins/skills/ ava_builtins/plugins/ .agents/skills/
```

External extensions (L1–L3) never appear in the kernel repo's log — a plugin
developed in its own repo would otherwise be invisible to this loop exactly
where user modification concentrates. Sweep them too:

```python
import json, os, subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
home = Path(os.environ.get("AVA_HOME") or Path.home() / ".ava")
cutoff = (datetime.now(UTC) - timedelta(days=1)).isoformat()
reg = json.loads((home / "installed.json").read_text())  # install registry
print([p["name"] for p in reg["packages"] if (p.get("updated_at") or "") >= cutoff])
for d in (home / "plugins").iterdir():                    # hand-cloned plugin repos
    if (d / ".git").is_dir():
        log = subprocess.run(["git", "-C", str(d), "log", "--since=7 days ago",
                              "--pretty=%h %cI %s"], capture_output=True, text=True).stdout
        if log.strip():
            print(d.name, "\n", log)
```

(When issue #39's cluster registry lands, its version-change feed replaces
this per-machine sweep.)

For each batch, list the skills/plugins whose files changed in the past day,
with dates — these are your suspects. On Monday, use the past week's changes.
If nothing changed, write a short report noting the dataset grew and stop early.

### 3. Mine the bad runs and analyze

```
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/ava-self-evolution/reference/mine.py
```

Clusters the active window's `failed`/`fumbled` runs and prints a markdown digest —
which run ids, what went wrong — in two passes: by the skill they touched, and
by the plugin contribution (`<plugin>/<surface>/<identifier>`) that fired in
them. The plugin pass reads `plugins_activated` — each time a plugin hook,
wrap, or prompt section actually acted in the run — so a silently-firing
hook's regression is attributable to the exact contribution rather than
invisible.

For each cluster that (a) has several bad runs AND (b) overlaps a skill/plugin
that changed in the active window, ask the original agents directly with
`evaluate.debrief()` (80% of signal), then spawn one deep-dive worker with
`ava.agents.spawn` for the remaining 20%:

- Give it the skill name, the run ids, the dataset path
  (`$AVA_HOME/self_evolution/dataset/<week>.jsonl`), and that skill's `git diff`
  for the active window.
- Task it: read the runs' `transcript` in the dataset file and the skill's
  SKILL.md + diff. Did this skill's change cause the failures? Name the root
  cause and the exact edit that would fix it; reply in 3-5 lines.
- The worker reads the dataset file directly — the full transcript is in it, no DB access.

Collect the workers' replies with `ava.agents.get_last_message`. Attribution
combines a logged `skill_invoked` signal with a content scan of the trace — a
strong suspicion, confirmed by the worker reading the real trace.

Write findings to `$AVA_HOME/self_evolution/proposals/<window>-<skill>.md`:
phenomenon, real run ids, root cause, concrete fix.

**Optional deep dive.** The dataset's transcript is usually enough. When a
finding hinges on something a transcript cannot show — where the time went, why
an exec died, what the agent's history looked like before a compaction — read
the run itself: `ava.help(ava.skills.inspect_a_trace)` is the correlation
know-how across the checkpoints table, the Loki event river, and the Tempo
spans, complementing `evaluate.debrief()` with evidence the agent's own account
does not carry; it costs a few queries, so reach for it per finding, not per
run.

### 4. Re-run to verify (optional, off by default)

Only when a specific proposal is worth confirming empirically. The dataset's
recorded outcome is the "old" baseline; re-running the same task under the
current tree is the "new" side — via the Evaluation Loop's spawn path
(`reference/evaluate.py`: `launch` -> `poll` -> `gather`). A clean A/B, not
a verbatim replay: a fresh agent gets only the task prompt — never the
original transcript — verified against the original tool profile
(`verify_replay`).

Only the **replay-safe subset** is ever re-run — tasks whose tool calls are
pure read/compute (the `is_replay_safe` gate in `evaluate.py`). The OS and
network are not sandboxed, so tasks that ran a shell command, sent a message,
edited files, or hit an external API are skipped. Skip this step unless a
proposal earns the cost. Gather verifies every completed
replay against its original tool profile; degenerate or side-effecting runs are
listed as `invalid` and excluded from the mean.

### 5. Report

Write `$AVA_HOME/self_evolution/reports/<window>.md`:

1. **Dataset** — runs collected in the batch or Monday summary (ok / fumbled / failed), batches accrued.
2. **Changes** — skills/plugins that changed in the active window.
3. **Findings** — per confirmed regression: skill, real run ids, root cause,
   the fix, and (if run) re-run scores. Mark fixes you are opening as PRs.
4. **No-signal changes** — changes with no related regression, so the next
   batch or Monday summary knows they were checked.

Then notify the user with `ava.ui.notify(title="Self-evolution: <N> changes, <M> suspected regressions", content="<report path>")`.

For a high-confidence skill fix, open a PR to `main` following the
`ava-self-development` skill's workflow (PR title `[Ava-<your-id>]`, commit
`Co-authored-by: Ava #<your-id>`). Do not merge it yourself — leave it for the
user to review.

## Evaluation Loop (optimizing skill text)

The report tells the user what regressed. The evaluation loop goes further: it
**optimizes the skill text itself**, like backpropagation optimizes weights.

```
dataset  = training data      (real tasks + traces)
rubric   = loss function      (completion + efficiency, in reference/rubric.py)
skill    = the weights        (the SKILL.md text under test)
iterate  = backpropagation    (measure -> propose edit -> re-measure -> keep the best)
```

For each skill that changed in the batch (or the full week on Monday), run this loop (2-3 rounds):

1. **Pick tasks.** From the dataset, take that skill's tasks (the `mine.py`
   clusters point at them). `evaluate.py` only spawns **replay-safe** ones, so
   curate a small representative set (2-3 tasks) of pure read/compute tasks.
   The case-selection standard (**strong / diverse / representative**) and the
   fine-grained anti-cheat trace audit are in the `evaluation` sub-skill — read
   it before setting up or scoring a case set.
2. **Baseline.** Score the current skill by re-running those tasks with fresh
   agents (see the async mechanics below). Record the mean `completion` /
   `efficiency` / `overall`.
3. **Propose.** Spawn an analysis worker to read the lowest-scoring traces plus
   the skill text, and propose one concrete edit to the SKILL.md.
4. **Edit.** Apply the proposed edit to the skill text (in a worktree).
5. **Re-measure.** Run `evaluate.py` again on the same tasks. If the mean
   `overall` went up, the edit is an improvement; if down, discard it.
6. **Iterate.** Repeat 3-5 for 2-3 rounds, keeping the best-scoring version.
   Open that as a PR for the user to review — never auto-merge.

### Running evaluate.py (async — spawn then gather)

A task run takes minutes, longer than one code block may run, so evaluation
is two phases with a wait in between. Import it from your own code (add the
reference dir to `sys.path`, then `import evaluate`; `launch` needs your live
agent identity, so it is not a CLI):

```python
import os, sys; sys.path.insert(0, os.path.join(os.environ["AVA_HOME"], "skills", "ava-self-evolution", "reference"))
import evaluate
state = evaluate.launch("ava-goal", tasks)   # spawns one fresh agent per safe task
```

Then wait for the eval agents to finish — launch a goal-watch
watcher on them. Each following turn, reload and check:

```python
import evaluate
state = evaluate.latest_state("ava-goal")
progress = evaluate.poll(state)          # {"done": [...], "pending": [...]}
# when pending is empty:
report = evaluate.gather(state)          # {"mean": {...}, "per_task": [...]}
```

Compare `report["mean"]["overall"]` before vs after your edit. `rubric.py`
scores two dimensions in [0, 1]: **completion** (output produced, no breach,
clean exec) and **efficiency** (few tokens/turns, no exec failures, no
compaction, no re-prompts); `overall` weights completion higher. Use only the
verified mean — invalid replays are reported separately.

## Data source

Events come from **Loki** via the gateway `/api/events` endpoint — PG `events`
is a frozen archive since 2026-08-12 (Task #1197 LGTM cutover); `collect.py`
is the Loki read path, and a 0-run dataset is an ALERT (exit 2), never
"nothing to act on" — except a TEST- only window (QA review of PR #698), which exits 0.

**Transcript completeness.** Each record's `transcript` is the checkpoint's
complete read path (`load_checkpoint_messages_full`, `shared/checkpoint.py`):
retained `compact_boundary` snapshots stitched with the latest segment,
dropping only the repeated leading system prompt; summaries and session notes
stay. "Complete" = full history since the compact-boundary retention rule
(Task #1125, effective 2026-08-10); earlier trimmed history is not
reconstructed — detail: the `evaluation` sub-skill.

## Daily threshold scan

The supervisor's daily scan (`self-evolution-daily`, 00:00 deployment timezone)
checks the runs accumulated since the last batch. At roughly 100 runs, it wakes
this agent to run the batch flow above; daily traffic of ~100-270 runs makes
that roughly daily. The batch command collects one day; Monday's summary
collects seven. Failed/fumbled runs, an unexplained empty window, a missing
scan, or a hard failure also wake this agent so a broken data source cannot hide.

**Loki-failure fallback.** On dense windows `/api/events` can 500; run
`reference/mirror_backfill.py <days> [week]` — collect from the local event mirror (rows deduped by event id).

## Cron integration

This skill has two supervisor-driven wakes on the gateway (managed through the
`ava schedules` CLI):

| run | trigger | collection |
|-----|---------|------------|
| batch flow | daily scan reaches ~100 accumulated runs | `collect.py --days 1` |
| Monday summary | `self-evolution-weekly`: Tuesdays 00:00 Asia/Shanghai = Mondays 09:00 PT | `collect.py --days 7` |

## Principles

- **The dataset is the deliverable.** Grow it every batch; the Monday summary
  covers the whole week. Analysis is built on it, not instead of it.
- **Read what happened; do not re-run by default.** A failure is already in the
  trace — you rarely need to reproduce it. Replay is a targeted verification
  step, not the main loop.
- **`ok` is not "good".** It only means no objective failure signal fired.
  Absolute quality of an organic run is not measured here.
- **Attribution is a suspicion.** Confirm it by reading the real trace before
  claiming a skill caused a regression.
- **Bias to a concrete fix.** A finding without a specific edit is not done.
- **Measure edits, don't guess.** In the evaluation loop, keep a skill edit
  only if the rubric score rises on the same tasks — the dataset is the judge,
  not your intuition.

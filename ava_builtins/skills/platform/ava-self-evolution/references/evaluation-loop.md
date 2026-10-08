# Evaluating a proposed skill edit

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Evaluation Loop (optimizing skill text)

The report tells the user what regressed. The evaluation loop goes further: it
**optimizes the skill text itself**, like backpropagation optimizes weights.

```
dataset  = training data      (real tasks + traces)
rubric   = loss function      (completion + efficiency, in ava_builtins/skills/platform/ava-self-evolution/scripts/rubric.py)
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
scripts dir to `sys.path`, then `import evaluate`; `launch` needs your live
agent identity, so it is not a CLI):

```python
import os, sys; sys.path.insert(0, os.path.join(os.environ["AVA_HOME"], "skills", "ava-self-evolution", "scripts"))
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

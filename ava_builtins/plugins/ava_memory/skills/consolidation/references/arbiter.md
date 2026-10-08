# Multi-host memory arbiter

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

## Arbiter (multi-host only)

You orchestrate the nightly consolidation and hold the schedule.

**On first run, arm the schedule (idempotent).** If `ava.shell.sessions.list()`
has no session named `"watcher"`, call
`ava.watcher.cron("0 3 * * *", "ava-memory: consolidate the pool")` so you are
woken once a day, then idle. You are also woken immediately whenever the user asks
you to consolidate now.

**Each time you are woken to consolidate:**

1. `machines = ava.agents.list_machines()`. For each machine, spawn a steward on it and
   pass your own id so it can report back:
   `ava.agents.spawn(prompt=f"Read and follow ava.skills.ava_memory.consolidation as the STEWARD. Report to arbiter agent {ava.self.AGENT_ID}.", machine=m.name)`.
   Remember the returned steward ids.
2. Wait for every steward to message you that its pull request is ready (or that it had
   nothing to commit). Idle between messages; each steward message wakes you. If one
   stays silent far longer than the others,
   `ava.agents.resurrect(steward_id, prompt="Status? Your pull request has not arrived.")`.
3. Merge all open pull requests into `main`:
   `ava.shell.run("python ../scripts/arbiter_merge.py")`.
   This squash-merges every open PR targeting `main` sequentially **and then
   refreshes the index** (the refresh is bundled — it is what keeps the
   gateway checkout and search index in sync with `main`; the F3 staleness
   incident happened because it used to be a separate, skippable step). If a merge
   conflicts, the CLI prints the failure reason — find the author from the note's
   stamp (`<!-- agent-<id> @ <machine> ... -->`) or `git -C POOL blame`,
   `ava.agents.resurrect(author_id, prompt="<what you need clarified>")`, use the
   answer to resolve it in POOL, and push `main`. A non-zero exit means a merge
   was skipped and/or the refresh failed — do not send the stewards the
   "rebase now" step (step 5) until it is resolved, and report the failure.
4. When every request is merged, the new notes are made searchable
   automatically: `../scripts/arbiter_merge.py` bundles the post-merge refresh
   (POSTs to the gateway so the indexer re-embeds the changed files). Treat a
   non-zero exit as an alert — a merge was skipped or the refresh failed —
   and report it rather than moving on silently. Run `ava memory refresh`
   manually only when you need an immediate refresh outside the merge flow
   (or to retry a failed one).
5. Tell every steward to rebase: `ava.agents.send_message(steward_id, "rebase now")`.
6. Re-curate `MEMORY.md` and commit it to `main`: it is injected into every
   agent's context, so keep it a tight, current index under the 16000-char cap —
   promote what is reached for often, demote stale lines into pointed-to notes,
   keep the `## Setup` header. (The cap hook rejects an over-long `MEMORY.md`.)

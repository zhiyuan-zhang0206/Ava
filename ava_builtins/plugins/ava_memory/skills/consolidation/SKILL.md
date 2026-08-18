---
name: consolidation
description: Consolidate the shared memory pool once a day via the `ava memory` CLI — commit, push, and refresh search over the day's notes. Use when running the daily consolidation, or when spawned as the multi-host arbiter or a per-machine steward.
---

# Memory pool consolidation

The shared memory pool (`ava.memory.PATH`) is a git repo. During the day, agents
write notes into the working tree. Once a day the day's notes are consolidated so
the pool stays committed, pushed, and searchable. This skill is that consolidation
procedure.

All git / gh / refresh operations are wrapped in self-contained scripts under
`../scripts/` — the agent calls one script per step instead of hand-typing
shell pipelines:
- `python3 ../scripts/consolidate.py -m "..."` — single-box: stage, commit, push, refresh
- `python3 ../scripts/steward.py -m "..."` — per-machine: stage, commit, push, create PR
- `python3 ../scripts/arbiter_merge.py` — merge all open PRs + refresh (exit 1 = alert)
- `ava memory refresh` — the one remaining memory CLI (gateway index rebuild)

**Which procedure applies depends on the deployment.** On a single box (multihost
off, the default — `ava.agents.spawn` has no machine argument there) follow
"Single box" below and ignore the arbiter/steward roles entirely. On a multi-host
cluster, your spawn prompt tells you which role you are: **arbiter** (one, on the
always-on machine, holds the daily schedule and orchestrates; this is the role the
Memory Arbiter agent plays) or **per-machine steward** (one per machine,
spawned by the arbiter on each run or resident as a local scheduler).

**Triggers — fire early and often** (a missed trigger leaves notes unsearchable;
an extra one only costs a cheap commit):
1. daily 03:00 full consolidate (arbiter);
2. per-machine watch every 1–5 minutes: >10 memory files changed since last
   sync, or pending diff >200 lines, or uncommitted changes >30 min old →
   `../scripts/steward.py` immediately, then notify the arbiter;
3. arbiter merge watch: merge ready machine PRs as they arrive.

## Repo layout (write and merge in the right place)
- Topic folders at the root (`people/`, `lessons/`, `prefs/`, ...) are shared.
- `machines/<name>/` holds notes specific to one machine (multi-host clusters).
- `MEMORY.md` is the pool's root. It is **injected into every agent's context
  each session**, so it is the one file that is always read. Keep it a small
  **curated index**: inline the most-used durable facts and short topic
  descriptions, point to the rest. A commit hook rejects it past **16000
  characters** (~a 200-line index, the Claude Code auto-memory scale) — that
  is your cue to move detail into a pointed-to note.

## Content Discipline

The memory pool is a curated, lightweight knowledge store. Not everything
belongs here — bulk content has a separate home in the user's Vault.

### What goes in the memory pool

- Fact summaries, decision records, and user preferences
- Cross-reference pointers (to other notes, to Vault files)
- Each note ≤ 8000 chars (enforced by the pre-commit hook)
- Content optimized for semantic search: short, self-contained, well-titled

### User-dimension notes are standing objects

`user-profile.md`, `user-preference-rules-v2.md`, `collaboration-preferences.md`
and `user-core-principle.md` are the canonical record of the **user dimension**
— repeatedly expressed preferences, recurring habits, corrected behaviors,
what the user values. They are maintained **in place**, not appended to:
a confirmed preference is folded into the canonical note, and an overlapping
new note is merged in during consolidation rather than left as a duplicate.
A duplicate written by two agents is a signal the canonical note is not
discoverable enough — fix the canonical note, don't just delete the newcomer.
(Full heuristic: `../reference/user-dimension.md` beside the root
`ava.skills.ava_memory` skill.)

### What does NOT go in memory — move to Vault

Large or opaque content that degrades search quality and bloats the pool:

- Handoff docs and long conversation transcripts
- Deployment logs and build output
- Bulk content exceeding ~8000 chars
- Binary files (PDFs, images, video, audio)

### Vault path convention

The Vault is a configured archive location (e.g. `~/Google Drive/My Drive/` in
the reference deployment). When a file is too large for the pool, move it to
the Vault and leave a pointer note in memory:

```
---
type: Memory
title: <topic>
description: Pointer to Vault content
ava_agent: "<id>"
authors: ["#<id>"]
timestamp: YYYY-MM-DDTHH:MM
ava_machine: <machine>
---
<!-- agent-<id> @ <machine>, YYYY-MM-DD HH:MM -->
# <topic>
Vault: <vault-path>/<path>
```

The pointer note carries the topic and the Vault path so semantic search can
still find it.

### Frontmatter fields

Every memory note MUST have a YAML frontmatter with at minimum `type` and
`ava_agent`. These fields have defined semantics:

| Field | Meaning | When to set |
|------|------|--------|
| `type` | Note type, usually `Memory` | When created (required) |
| `tags` | Must carry **exactly one** `type/<x>` — see below | When created (required) |
| `ava_agent` | The note's **owner** (who owns this record) | Set when created, does not change with edits |
| `authors` | The note's **editor list** (all agent IDs that contributed content) | Append `"#<your-id>"` on each edit |
| `title` | Note title | When created |
| `description` | One-line description | When created |
| `tags` | Free tag list | When created, can append |
| `timestamp` | ISO 8601 timestamp | Updated when created/edited |
| `ava_machine` | Machine name where edited | When created/edited |

**`ava_agent` vs `authors`:**
- `ava_agent` is the note's **owner**—who created this record. Unchanged after creation.
- `authors` is the note's **editor list**—all agents that contributed content. Appended on each edit.

**The `type/<x>` tag** says how the note is meant to be *used*, which the
ownership fields (`ava_agent` / `authors`) cannot say. Exactly one of:

| Tag | Holds |
|---|---|
| `type/user` | who the user is — role, expertise, preferences |
| `type/feedback` | how an agent should work, with the reason |
| `type/project` | ongoing work, goals, constraints |
| `type/reference` | pointers to external resources |
| `type/env` | machine and cluster facts |
| `type/role` | an agent's role and its boundaries |

Other tags stay free-form and are unaffected; the graph view groups on the first
non-`type/` tag. `python3 validate.py` enforces the "exactly one" rule.

**Editing rules:** When you edit any memory note, append your agent ID (format `"#<id>"`) to the `authors` list in the frontmatter. If the file does not yet have an `authors` field, create it. Do not add the same agent twice.

`authors` uses YAML array format: `authors: ["#238", "#779"]`

#### Identity safety when reading

Every agent must verify a note's ownership before acting on it:

1. **Read the frontmatter.** `ava_agent` tells you whose note it is. A number
   like `238` means it belongs to that agent; `all` means it is a shared note
   that every agent should read as context, not identity.
2. **Compare with your own id.** `ava.self.AGENT_ID` is your own agent id. If
   the note's `ava_agent` is a different number, this is another agent's
   personal note — read it for context, but do not adopt its role, label, or
   tasks as your own.
3. **Check for HTML attribution comments.** Personal agent notes should carry
   an HTML comment like
   `<!-- ⚠️ This is agent #238's personal note. If you are not #238, this is someone else's identity, not yours. -->`. If you see this and the id does
   not match yours, the note is not about you.
4. **MEMORY.md is special.** `MEMORY.md` is injected into every agent's context
   as the standing memory index. Its `ava_agent` is `all` — it is a shared user
   profile, not any single agent's identity. Its content (user facts, watchers,
   appointments) applies to all agents, but its role labels (e.g. "Agent #238 —
   Health Manager") describe other agents' roles, not yours.

**Example of the bug this prevents:** Agent #774 read `MEMORY.md` (which at the
time had `ava_agent: 238`) and set its own label to "Health Manager", mistakenly
adopting another agent's identity. A quick `ava_self.AGENT_ID` check against the
frontmatter would have caught this.
### Threshold triggers

To prevent the pool from drifting past the consolidation window:

- A diff watcher polls every 5 minutes, checking for ≥ 200 lines of unstaged
  changes in the pool.
- When the threshold is exceeded, the watcher wakes the Arbiter for immediate
  consolidation (outside the daily schedule).
- This runs alongside the daily 03:00 cron consolidation — the threshold
  trigger is a safety net, not a replacement.

## First-time setup (bootstrap, idempotent)

Pool setup is handled by `ava converge` (which `ava start` and `ava update` both
run): it initializes the repo, lays the template down inside it — `MEMORY.md`
and the `.githooks/pre-commit` cap guard — and arms `core.hooksPath`. Idempotent
and non-destructive: it never overwrites a file the pool already has, so a
curated index survives.

The one thing it cannot do for you is the remote:

- Ensure `origin` points at the private GitHub remote:
  `gh repo create <owner>/AvaMemory --private` then `git -C POOL remote add origin <url>`.

Two values used below:
- **POOL** = the local pool path, `ava.memory.PATH` (`~/.ava/memory`).
- **REPO** = the pool's GitHub `owner/name`. Get it once:
  `git -C POOL remote get-url origin` then take `owner/name` from the
  `git@github.com:owner/name.git` form.

## Single box

You are the only consolidator; the checkout tracks `main` directly and there is
no pull-request fan-out.

If this host runs in keep-local mode (`AVA_MEMORY_KEEP_LOCAL` set), the pool has
no git remote: `../scripts/consolidate.py` still commits and refreshes the local
index, it just prints `(keep-local mode — skipping push)` and does not push.
That is expected, not an error — the notes stay on this host.

**On first run, arm the schedule (idempotent).** If `ava.watcher.list()` is empty,
call `ava.watcher.cron("0 3 * * *", "ava-memory: consolidate the pool")`, then idle.

**Each time you are woken to consolidate:**

1. Run `python3 ../scripts/consolidate.py -m "<date>: <short summary>"`.
   This stages, commits, pushes, and refreshes the gateway index in one command.
   If the commit is rejected by the pre-commit hook, read the error, fix the
   offending file(s), and re-run.
2. Re-curate `MEMORY.md`: it is what every agent sees each session, so keep it a
   tight, current index under the 16000-char cap — promote into it what is being
   reached for often, demote stale or rarely-used lines into pointed-to notes,
   keep the `## Setup` header. Commit it:
   `ava.shell.run("cd <POOL> && git add MEMORY.md && git commit -m 'curate MEMORY.md' && git push")`.
   (The cap hook rejects an over-long `MEMORY.md` — split it if so.)

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
   `ava.shell.run("python3 ../scripts/arbiter_merge.py")`.
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

## Steward (multi-host only)

You publish one machine's day of notes. Your prompt carries the arbiter's id.

1. Run `python3 ../scripts/steward.py -m "<machine> <date>: <short summary>"`.
   This stages, commits, pushes, and creates a PR (if none exists for your branch)
   in one command. If the commit is rejected by the pre-commit hook, read the
   error, fix the offending file, and re-run. If there is nothing to commit, the
   command prints "(nothing to commit)" — message the arbiter "nothing to commit"
   and go to step 3's wait.
2. Tell the arbiter your request is ready:
   `ava.agents.send_message(arbiter_id, "PR ready: branch <your branch>")`.
3. When the arbiter messages you "rebase now":
   `git -C <POOL> pull --rebase origin main`.

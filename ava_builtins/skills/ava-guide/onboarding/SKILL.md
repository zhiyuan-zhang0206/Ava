---
name: onboarding
description: First-use onboarding for a new cluster user — interview preferences (language, notification channel, timezone, update rhythm, confirmation gates, reporting style), discover intent, record it all to memory, and start the first piece of work. Use when the pool has no type/user note for the person, they say they just installed Ava or ask "what can you do", someone new joins, or they ask how to migrate from Claude Code, Codex, OpenClaw or Hermes.
---

# Onboarding — First Use of a Cluster

A new cluster starts empty: no user profile in the memory pool, no standing
tasks, no roles. The first conversation is the only chance to fill the two
things every later turn depends on: **who the user is** (memory notes) and
**what the user wants the cluster for** (the first piece of work). This
sub-skill is that conversation, start to finish.

Unlike the other ava-guide sub-skills, this one is not about the `ava` CLI —
it is about the user. It lives here because a fresh cluster's first question
is "now what?"

## When this applies

Run onboarding when any of these hold:

- The memory pool has no `type/user` note for the person you are talking to
  (`ava.memory.search(...)` for their name, read `MEMORY.md`).
- The user says they just installed the cluster, are new to Ava, or asks
  "what can you do?"
- A new person joins an existing cluster (handover, collaborator).
- The user is coming from another agent tool (Claude Code, Codex CLI, OpenClaw,
  Hermes Agent) and asks how their setup maps to Ava — see
  [Migrating from another tool](#migrating-from-another-tool).

Skip the interview when a current profile exists — go straight to work and
fill only the gaps you notice (a missing timezone, an unrecorded gate).

## End state

Onboarding is finished when **all four** are true:

1. The pool carries one `type/user` note (who the user is) plus the
   `type/feedback` notes (how to work with them) — see
   [Record to memory](#record-to-memory).
2. One concrete piece of work is underway: a task created, a worker
   spawned, or a schedule set.
3. The user knows the two ways to reach you — the live dialog and
   `ava.ui.notify` — and which one fires when.
4. Every preference you asked about is written down, so the user never
   answers the same question twice.

## Flow

### 1. Check memory before asking anything

Never interview cold. Read `ava.memory.PATH/MEMORY.md` and search the pool
for `type/user` notes first. A profile exists → skip to step 3, ask only
about missing fields. Nothing exists → run the full flow.

### 2. Preference interview

Ask in small batches — two to four questions per message — and **write each
answer down as it arrives**, not at the end. Order by how much later work
depends on the answer:

1. **Language** — which language should every agent use when talking to
   you?
2. **Notification channel** — live replies in the dialog; async
   `ava.ui.notify` when you are away. Which kinds of things deserve
   `require_response` (decisions only you can make)?
3. **Timezone** — schedules, reminders, and deadlines anchor to this.
4. **Update rhythm** — how fast the cluster takes updates (`ava cluster
   update`): every release, on a schedule, or manual; plus maintenance
   windows to avoid.
5. **Confirmation gates** — which actions need the user's OK before
   running: irreversible, outward-facing, spending real money, reaching a
   real person.
6. **Reporting style** — detail level, progress-update frequency, whether
   finished work is served as a page (`ava.ui.serve`).

Exact wording in [Question list](#question-list).

### 3. Discover intent

Ask what the user wants the cluster for, then follow the matching branch in
[Intent branches](#intent-branches).

### 4. Record to memory

Write the notes while the answers are fresh — see
[Record to memory](#record-to-memory).

### 5. Start the first piece of work

End with one small, visible thing running — never "tell me when you need
something". Create the task, spawn the worker, or set the schedule, then
state plainly what is running and when the user will hear about it.

### The conversation, turn by turn

A recommended skeleton for the whole onboarding; compress when the user is
impatient, but keep the order.

| Turn | You say | What happens behind it |
|---|---|---|
| 1 | Greeting + "let me check what I already know about you" | Read the pool index, search `type/user`. This is the fork: profile found → jump to turn 4. |
| 2 | Language + notification channel + timezone | First batch of questions (see below). Write answers as they come. |
| 3 | Update rhythm + confirmation gates + reporting style | Second batch. Write answers as they come. |
| 4 | "What do you want this cluster for?" | Branch A–D below; restate the goal or proposal in one sentence and confirm it. |
| 5 | "Here is the first thing I am starting: …" | Kick off the first task, state when they hear back. Onboarding ends here. |

## Question list

Wording below is a floor, not a script — ask naturally, but capture every
group.

### Language

- "What language should I use when talking to you?"
- Follow up only if ambiguous: "Same for reports and pages, or different?"
- Record: `type/user` (an attribute of the person); add `type/feedback` if
  the user states it as a rule ("always Chinese, including reports").

### Notification channel

- "When you are not in this dialog, how should I reach you — a notice in
  the queue, or something else?"
- "Which decisions should I wait for your OK on instead of deciding
  myself?" (Expect: irreversible actions, outward-facing actions, spending
  real money, contacting real people.)
- Record: `type/feedback` — it is how you work, with the reason the user
  gave.

### Timezone

- "Which timezone should schedules and reminders use?" Offer to derive it
  from their location if they are unsure.
- Record: `type/user`; add `type/feedback` if it changes how you schedule
  (e.g. "never schedule anything before 9 AM local").

### Update rhythm

- "How fast should this cluster take updates — as soon as a release lands,
  on a schedule, or only when you say?"
- "Are there hours when I should avoid maintenance?"
- Record: `type/feedback` (and `type/env` if the window is a machine fact).

### Confirmation gates

- "What should I never do without checking with you first?"
- Record: `type/feedback` — a standing rule, not a one-off.

### Reporting style

- "Do you want finished work as a served page, a short chat summary, or
  both? How often should progress updates arrive while something is
  running?"
- Record: `type/feedback`.

## Intent branches

The answer to "what do you want this cluster for?" falls into one of four
branches. Follow the branch; if the user is undecided, walk branch B until
they land somewhere.

### A. A concrete goal ("track my health", "watch this company")

1. Restate the goal in one sentence and ask "is this the target?" — lock
   the target before proposing anything.
2. Ask what done looks like and the constraints (cadence, budget, what not
   to touch).
3. Record goal + constraints as `type/project`.
4. Decompose into tasks. If it is large, load `ava.skills.ava_workflow`
   (calibrate → align → plan) and `ava.skills.ava_fleet` for
   parallelization. For the first task, one small real step beats a grand
   plan: create the task (`ava.tasks.create`) or spawn the first worker,
   and tell the user what is running.

### B. "What can you do?" / vague

1. Do not recite the skill catalog. Show one capability on the user's own
   material — "paste a link and I will summarize it", "give me a topic and
   I will research it". One live demo beats a tour.
2. Ask what they spend their time on; route the demo toward that.
3. End by proposing the first small task drawn from what they mentioned,
   and start it.

### C. Ongoing services ("keep an eye on X", "manage my Y")

1. For each domain they name, propose one dedicated role agent — long-
   running, owns that domain, reports on a cadence.
2. Agree each role's boundary before spawning: what it owns, what it may
   never touch.
3. Record each role as `type/role` with its boundary.
4. For time-triggered work, load `ava.skills.ava_schedule_writer` and
   create the schedule; spawn the first role agent with a self-contained
   prompt naming the domain and the cadence.

### D. Evaluating Ava itself

1. Explain in one paragraph: agents + one tool (`execute_code`) + skills +
   memory; you are one agent in a fleet, peers get spawned per task.
2. State honest limits: you can be wrong; irreversible and outward-facing
   actions always ask first; skills are instructions you read, not
   guarantees.
3. Offer a contained trial: one small task, a clear success criterion, no
   standing commitments. If the trial succeeds, treat it as branch A.

## Migrating from another tool

A user arriving from another agent tool is not a blank slate — they bring
skills, configs, automations, and habits shaped by the old tool. Give them
the map instead of re-interviewing from scratch. One page per source tool:

| Coming from | Read |
|---|---|
| Claude Code (Anthropic) | [migrate-from/claude-code.md](migrate-from/claude-code.md) |
| OpenAI Codex CLI | [migrate-from/codex.md](migrate-from/codex.md) |
| OpenClaw (ex Clawdbot/Moltbot) | [migrate-from/openclaw.md](migrate-from/openclaw.md) |
| Hermes Agent (Nous Research) | [migrate-from/hermes.md](migrate-from/hermes.md) |

Each page has the same shape: a concept-map table (their concept → its Ava
equivalent), migration steps, and the honest differences and pitfalls, with
sources. Four facts cut across all four: skills port directly (same Agent
Skills standard), instructions become memory (`type/feedback`), automations
become `ava schedules` / `ava.watcher`, and every tool's permission system
becomes recorded gates plus `require_response` notices. Read the page for the
tool they name, then walk it with them.

After the map, run the standard flow — intent discovery, record to memory,
first piece of work. The migration pages replace only the "where does my old
stuff go" part of the conversation.

## Record to memory

User preferences go into the **shared pool** (`ava.memory.PATH`), never only
personal memory — every agent must see them. Write with absolute paths: a
relative path resolves against `ava.cwd`, not your workspace, and the note
lands in the wrong directory.

Pool note template (fields the pool validator requires):

```markdown
---
type: memory
title: <short title>
description: <one line — the only thing a pointer/search result shows>
tags: [type/<x>, <extra tags>]      # exactly one type/ tag
timestamp: 'YYYY-MM-DDTHH:MM:SS+00:00'
ava_agent: all
authors:
- '#<your agent id>'
ava_machine: <your machine name>
---
<!-- agent-<your id> @ <your machine>, YYYY-MM-DD HH:MM -->

<body>
```

### Which tag takes what

| You learned | Tag | File | Example |
|---|---|---|---|
| Who the user is — name, contact, language, timezone, accounts | `type/user` | `<pool>/user-profile.md` (one consolidated note) | "User is on Beijing time" |
| How to work with them — channels, gates, cadence, corrections | `type/feedback` | one note per rule or per cluster of related rules | "Serve pages, never email reports" |
| An ongoing goal and its constraints | `type/project` | `<pool>/projects/<slug>.md` | "Track competitor X, weekly" |
| A role an agent was given and its boundary | `type/role` | `<pool>/agents/<name>.md` | "Health steward: owns health domain only" |
| A machine or cluster fact discovered | `type/env` | `<pool>/infra/...` | "Backups run at 3 AM" |
| A pointer to an external resource | `type/reference` | `<pool>/...` | "Their Notion workspace URL" |

A `type/feedback` body leads with the rule, then the reason, then how to
apply it:

```markdown
## Rule
<the rule>

Why: <what the user said, or what broke when this was ignored>
How to apply: <when it fires and what to do>
```

Keep agent-private workflow state (your own checklist, drafts) in your
personal `memory/` instead — the pool is for facts other agents need.

## Anti-patterns

- **The questionnaire dump.** All six groups in one message reads as an
  interrogation; the user answers short and you learn less. Two to four
  questions per message, and write answers down as they arrive.
- **Re-asking.** Interviewing before checking the pool, or not writing
  answers down, makes the user repeat themselves. The pool is the whole
  point.
- **Personal-memory-only.** Preferences recorded only in `memory/` are
  invisible to every other agent; the next spawn re-asks.
- **Ending on "anything else?".** Onboarding ends with work running, not an
  open floor. The open floor produces silence, not tasks.
- **One-off scope.** Treating "don't email me, serve pages" as a one-time
  instruction instead of writing the rule means re-learning it per task.

# Recording onboarding context

Read this reference only for the task or mode selected by the skill entrypoint.
Run command examples from the original skill directory unless they specify
another working directory.

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

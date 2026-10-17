---
type: doc
title: Skills catalog
description: The full catalog of repo skills under meiva_builtins/skills/, grouped by what each does — communication, ops/scheduling/lifecycle, orchestration, self-improvement, web/media — one line per skill with a link to its own OKF node.
tags:
- extensions
- agent-instruction
---

# Skills catalog

Each repo skill is a self-contained directory under `meiva_builtins/skills/<group>/<name>/`
(`SKILL.md` + optional `scripts/` for runnable code, `references/` for
material the agent reads and adapts, `assets/` for templates and static
files). The source-tree groups are `practice/` (engineering, research, review),
`coordination/` (running and supervising agents, schedules), `platform/`
(operating, extending and documenting Meiva itself) and `integrations/`
(mail, SMS, Telegram, audio, web); a group is a folder without a `SKILL.md`,
and converge lands each skill flat at `skills/<name>/`, so the group never
reaches a skill's identity. This index lists the skills that carry their own
OKF node, grouped by what each does — those headings cut across the source-tree
groups and are informational only.

Descriptions identify the capability and the situations that need it; keyword
mentions alone do not select an operational workflow. Entrypoints retain shared
decisions and constraints, while substantial mode procedures, examples, and
source context load from references only when relevant. Catalog audits follow
the criteria and evaluation cases owned by `skill-creator/references/audit.md`.

## Communication & user interaction
Launch web pages to display content or collect structured replies, read SMS
verification codes on macOS, and a full Gmail client.

| Skill | Purpose | Detail |
|-------|---------|--------|
| meiva-guide.pages | Compose page publishing and user input with reusable frontend resources | [[meiva_builtins/skills/platform/meiva-guide/pages/docs/pages.meiva.okf.md]] |
| sms | Read SMS/iMessage verification codes via macOS Messages.app (on-demand script, not a daemon) | [[meiva_builtins/skills/integrations/sms/docs/sms.meiva.okf.md]] |
| gmail | Full Gmail client (read/search/send/reply/forward/draft + newsletter, pure stdlib IMAP/SMTP) | [[meiva_builtins/skills/integrations/gmail/docs/gmail.meiva.okf.md]] |

## Ops, scheduling & lifecycle
Operating and extending Meiva, scheduling timed tasks, background watchers, and
behavioral discipline for long-running agents.

| Skill | Purpose | Detail |
|------|------|------|
| meiva-guide | Shared guide for humans and internal/external agents: deploy, operate, extend, schedule, and manage agents; root routes to sub-skills | [[meiva_builtins/skills/platform/meiva-guide/docs/meiva-guide.meiva.okf.md]] |
| impersonator-guide | Executor manual for an active Meiva takeover, including inherited context, receipt, and release | [Skill](../platform/impersonator-guide/SKILL.md) |
| meiva-being-a-long-running-agent | Operating as a long-running process: manage lifecycle, wait for external events, persist before compaction | [[meiva_builtins/skills/coordination/meiva-being-a-long-running-agent/docs/meiva-being-a-long-running-agent.meiva.okf.md]] |

## Orchestration & workflow
Breaking large tasks into multi-agent / long tasks and driving them.

| Skill | Purpose | Detail |
|------|------|------|
| meiva-workflow | Select working strategy across alignment, capability and timescale, goals, planning, coordination, and evaluation | [[meiva_builtins/skills/practice/meiva-workflow/docs/meiva-workflow.meiva.okf.md]] |
| meiva-dynamic-workflow | Orchestrate parallel workers: explore→fork→join→reduce | [[meiva_builtins/skills/coordination/meiva-dynamic-workflow/docs/meiva-dynamic-workflow.meiva.okf.md]] |
| meiva-goal | Sustain a terminal goal with evidence checks and optional peer supervision | [[meiva_builtins/skills/coordination/meiva-goal/docs/meiva-goal.meiva.okf.md]] |

## Self-improvement
Meiva improving itself — mining regressions, reviewing/creating skills, and
tech debt.

| Skill | Purpose | Detail |
|------|------|------|
| meiva-self-evolution | Weekly collect real runs into trace dataset, mine skill/plugin regressions and produce fix reports | [[meiva_builtins/skills/platform/meiva-self-evolution/docs/meiva-self-evolution.meiva.okf.md]] |
| skill-creator | Create / improve / review skill | [[meiva_builtins/skills/platform/skill-creator/docs/skill-creator.meiva.okf.md]] |
| sweeper | Tech debt sweep engine (reconcile repo debt tracker, land PR) | [[meiva_builtins/skills/practice/sweeper/docs/sweeper.meiva.okf.md]] |

## Web & media
Fetching content from the web and media — driving AI web apps, per-source
adapters, transcription.

| Skill | Purpose | Detail |
|------|------|------|
| web-ai | Drive ChatGPT/Gemini/Claude/Perplexity via logged-in browser; subs: console / deep-research / media | [[meiva_builtins/skills/integrations/web-ai/docs/web-ai.meiva.okf.md]] |
| web-sources | Fetch content from any source; per-platform adapter subs: generic / rss / youtube | [[meiva_builtins/skills/integrations/web-sources/docs/web-sources.meiva.okf.md]] |
| audio-transcribe | Transcribe audio/video / YouTube / URL to text (OpenAI, requires ffmpeg) | [[meiva_builtins/skills/integrations/audio-transcribe/docs/audio-transcribe.meiva.okf.md]] |

## Not indexed here
The remaining top-level built-ins (such as `meiva-corp`, `meiva-deep-research`,
`meiva-serious-engineering`, `meiva-serious-research`, and `telegram-send-file`)
have no OKF node; their `SKILL.md` is the reference. Capability/timescale
calibration belongs to `meiva-workflow`; deployment, incident operations,
modification layers, installation, plugin development, schedules, and external
agent execution and page composition belong to `meiva-guide`. Temporary waiting
contracts belong to `meiva.watcher`; waiting and recovery methods belong to
`meiva-being-a-long-running-agent`. Model speed and feedback settings belong to `meiva-guide`'s models skill.

## Key dependencies
- [[meiva/skills/docs/skills.meiva.okf.md|Skill System]] — skill mechanism and core-vs-instance origin axis

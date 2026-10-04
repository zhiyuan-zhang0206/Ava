---
type: doc
title: Skills catalog
description: The full catalog of repo skills under ava_builtins/skills/, grouped by what each does — communication, ops/scheduling/lifecycle, orchestration, self-improvement, web/media — one line per skill with a link to its own OKF node.
tags:
- extensions
- agent-instruction
---

# Skills catalog

Each repo skill is a self-contained directory under `ava_builtins/skills/<group>/<name>/`
(`SKILL.md` + optional `scripts/` for runnable code, `references/` for
material the agent reads and adapts, `assets/` for templates and static
files). The source-tree groups are `practice/` (engineering, research, review),
`coordination/` (running and supervising agents, schedules), `platform/`
(operating, extending and documenting Ava itself) and `integrations/`
(mail, SMS, Telegram, audio, web); a group is a folder without a `SKILL.md`,
and converge lands each skill flat at `skills/<name>/`, so the group never
reaches a skill's identity. This index lists the skills that carry their own
OKF node, grouped by what each does — those headings cut across the source-tree
groups and are informational only.

## Communication & user interaction
Launch web pages to display content or collect structured replies, read SMS
verification codes on macOS, and a full Gmail client.

| Skill | Purpose | Detail |
|-------|---------|--------|
| ava-ui | Launch web pages to display content / collect replies (markdown+LaTeX, choice/confirm/form/compare panels) | [[ava_builtins/skills/platform/ava-ui/docs/ava-ui.ava.okf.md]] |
| sms | Read SMS/iMessage verification codes via macOS Messages.app (on-demand script, not a daemon) | [[ava_builtins/skills/integrations/sms/docs/sms.ava.okf.md]] |
| gmail | Full Gmail client (read/search/send/reply/forward/draft + newsletter, pure stdlib IMAP/SMTP) | [[ava_builtins/skills/integrations/gmail/docs/gmail.ava.okf.md]] |

## Ops, scheduling & lifecycle
Operating and extending Ava, scheduling timed tasks, background watchers, and
behavioral discipline for long-running/ultra-speed agents.

| Skill | Purpose | Detail |
|------|------|------|
| ava-guide | Operate / extend yourself via the `ava` CLI; root SKILL.md is an index, seven bare-name sub-skills bear the load (`ops`, `mcp`, `packages`, `agents`, `presets`, `models`, `onboarding`) | [[ava_builtins/skills/platform/ava-guide/docs/ava-guide.ava.okf.md]] |
| ava-schedule-writer | Natural language → gateway managed scheduled task (resumable script + `/api/schedules`) | [[ava_builtins/skills/coordination/ava-schedule-writer/docs/ava-schedule-writer.ava.okf.md]] |
| ava-watcher | Start a background watcher that wakes you on event/time triggers (stop in-turn polling) | [[ava_builtins/skills/coordination/ava-watcher/docs/ava-watcher.ava.okf.md]] |
| ava-being-a-long-running-agent | Operating as a long-running process: manage lifecycle, wait for external events, persist before compaction | [[ava_builtins/skills/coordination/ava-being-a-long-running-agent/docs/ava-being-a-long-running-agent.ava.okf.md]] |
| ava-ultra-speed | Speed discipline for ultra-fast turnover workers: report as you go, never wait silently | [[ava_builtins/skills/coordination/ava-ultra-speed/docs/ava-ultra-speed.ava.okf.md]] |

## Orchestration & workflow
Breaking large tasks into multi-agent / long tasks and driving them.

| Skill | Purpose | Detail |
|------|------|------|
| ava-workflow | Three actors (agents / human / real world), three phases (Calibrate / Align / Plan) with Evaluation threaded through all of them | [[ava_builtins/skills/practice/ava-workflow/docs/ava-workflow.ava.okf.md]] |
| ava-dynamic-workflow | Orchestrate parallel workers: explore→fork→join→reduce | [[ava_builtins/skills/coordination/ava-dynamic-workflow/docs/ava-dynamic-workflow.ava.okf.md]] |
| ava-goal | Supervise another agent to achieve a goal (watcher wakes up on target idle to judge) | [[ava_builtins/skills/coordination/ava-goal/docs/ava-goal.ava.okf.md]] |
| ava-use-other-agents | Drive Claude Code / OpenAI Codex CLI for long tasks; hand the agent's identity to Codex, Claude Code or DeepSeek Harness | [[ava_builtins/skills/coordination/ava-use-other-agents/docs/ava-use-other-agents.ava.okf.md]] |

## Self-improvement
Ava improving itself — mining regressions, reviewing/creating skills, and
tech debt.

| Skill | Purpose | Detail |
|------|------|------|
| ava-self-evolution | Weekly collect real runs into trace dataset, mine skill/plugin regressions and produce fix reports | [[ava_builtins/skills/platform/ava-self-evolution/docs/ava-self-evolution.ava.okf.md]] |
| skill-creator | Create / improve / review skill | [[ava_builtins/skills/platform/skill-creator/docs/skill-creator.ava.okf.md]] |
| sweeper | Tech debt sweep engine (reconcile repo debt tracker, land PR) | [[ava_builtins/skills/practice/sweeper/docs/sweeper.ava.okf.md]] |
| auto-review | Automatic PR semantic review (AGENTS.md compliance, doc sync, security, test judgment) | [[ava_builtins/skills/practice/auto-review/docs/auto-review.ava.okf.md]] |

## Web & media
Fetching content from the web and media — driving AI web apps, per-source
adapters, transcription.

| Skill | Purpose | Detail |
|------|------|------|
| web-ai | Drive ChatGPT/Gemini/Claude/Perplexity via logged-in browser; subs: console / deep-research / media | [[ava_builtins/skills/integrations/web-ai/docs/web-ai.ava.okf.md]] |
| web-sources | Fetch content from any source; per-platform adapter subs: generic / rss / youtube | [[ava_builtins/skills/integrations/web-sources/docs/web-sources.ava.okf.md]] |
| audio-transcribe | Transcribe audio/video / YouTube / URL to text (OpenAI, requires ffmpeg) | [[ava_builtins/skills/integrations/audio-transcribe/docs/audio-transcribe.ava.okf.md]] |

## Not indexed here
The rest of `ava_builtins/skills/` (e.g. `ai-capability-timescale`,
`ava-corp`, `ava-deep-research`, `ava-modification-layers`,
`ava-package-installer`, `ava-qa-inspection`, `ava-serious-engineering`,
`ava-serious-research`, `develop-a-plugin`, `telegram-send-file`) carries no
OKF node — `SKILL.md` in each is the reference.

## Key dependencies
- [[ava/docs/skills.ava.okf.md|Skill System]] — skill mechanism and core-vs-instance origin axis

---
type: doc
title: Agent Skills Standard Compatibility
description: Ava's skill format is the agentskills.io open standard — which frontmatter fields Ava honors vs preserves-but-ignores, the three source layouts `ava skill install` accepts, and what still fails fast.
tags:
- extensions
- compatibility
- agent-instruction
---

# Agent Skills Standard Compatibility

## What It Is
Ava's skill format **is** the [Agent Skills](https://agentskills.io) open standard — `SKILL.md` (YAML frontmatter + markdown body) in a directory that may bundle `scripts/` / `references/` / `assets/`. An unmodified skill folder from Claude Code, Codex, opencode, Cursor or any other client of the standard installs and loads here as published; a skill authored here runs there. There is no Ava manifest, no conversion step, no wrapper.

## Frontmatter Fields
| Field | Ava |
|---|---|
| `name` (required) | **honored** — the tree key. Hyphens become underscores in the `ava.skills` attr path (`code-review` → `ava.skills.code_review`); a name that is not a Python identifier (e.g. leading digit) is still reachable via `getattr` |
| `description` (required) | **honored** — the index line an agent reads to decide whether to reach for the skill (see [[system-prompt.ava.okf.md]]) |
| `license` / `compatibility` / `metadata` / `allowed-tools` | **preserved, not acted on** — parsed, kept on disk, and rendered in the SKILL.md body the agent reads. Ava enforces no tool allowlist: its single tool is `execute_code`, so `allowed-tools` gates nothing here |
| any other key | **preserved, not acted on** — a client's own extension key is not an error |

Only genuinely malformed input fails: no `---` fence, invalid YAML, or a missing `name` / `description`. Encoding is normalized rather than rejected (`shared/frontmatter.py`: leading UTF-8 BOM dropped, CRLF/CR folded to LF, closing fence may end the file) — a skill authored on Windows is still a valid standard skill, so the encoding must not be what refuses it.

Two Ava-side notes for authors:
- The standard's bundled directories carry no SKILL.md, so they stay plain files the agent reads. A **nested** SKILL.md is how Ava gets sub-skills — a superset of the standard, which other readers simply see as a folder.
- Ava's own repo-shipped skills are additionally held to an 80-unit `description` ceiling by `scripts/lint_skill_descriptions.py`. That is a house prompt-budget rule at merge time, not a load-time constraint on installed skills (the standard's cap is 1024 characters).

## Install Layouts
`ava skill install <git-url-or-path> [--path SUBDIR] [--ref REF]` reads three source shapes (`cli/commands/_skill_package.py`), tried in order:
1. **bare skill** — SKILL.md at the source root; the tree is one package.
2. **collection root** — `skills/`, then `.claude/skills/`, then `.agents/skills/`; first one that exists and holds a skill wins (a repo carrying two usually carries the same skills twice).
3. **bare collection** — the root's visible child dirs are skill packages. Hidden dirs are skipped, so `.git` / `.github` never read as one.

A tree matching none of the three is an error, not a no-op install. Each discovered package installs whole — sub-skills and bundled files included — under its frontmatter name, one registry entry each.

Every install runs **two gates over all discovered packages** (`cli/commands/_skill_package.py`): destination collisions (fail fast) and a **security scan** (`shared/skill_scan.scan_package`) — critical findings block the install unless `--accept-risk` is passed; non-critical findings render as a report for the operator to read. The scan result is recorded in the registry entry (`scanned_at`), and `ava skill trust` promotion is a human judgment layered on top of a clean scan, not a substitute for reading it, so `ava skill enable` / `disable` stays per-skill. Destinations are pre-flighted before the first copy, so a collision aborts the whole install rather than half-populating the load dir. Unlike the runtime scan (which skip-warns past a broken third-party skill so one bad file cannot crash unrelated agents at system-prompt build), an explicit install refuses a malformed SKILL.md — the user is present to hear about it. A local path is read in place, never moved.

`ava plugins install` stays the entry point for a Claude Code **plugin** bundle (`.claude-plugin/plugin.json` with agents / commands / `.mcp.json`, of which skills are one part); its bare-skill case shares this module's copy + registry write.

## Key Dependencies
- [[okf/skills/skills.ava.okf.md|Skill System]] — the skill system this compatibility claim is about
- `shared/frontmatter.py` — the `---` parser both the runtime loader and the merge-time lint use
- `shared/install_registry.py` — the per-machine origin/enabled registry each installed package lands in

## Entry Points
- `cli/commands/_skill_package.py` — source-layout discovery + copy/register
- `cli/commands/skill.py` — `ava skill install / enable / disable / register`
- `ava/skills.py` — `_parse_frontmatter` (required-field gate over the shared parser), `_mount` (folder tree → namespace tree)

## Notes
- The standard is a *format* contract, not a runtime one: it says nothing about how an agent is given the skill. Ava's progressive disclosure (description in the system prompt, body pulled on demand via `ava.help`) is its own choice, and is what makes a large installed set affordable.
- Tests asserting the claim end-to-end (install → registry → namespace mount) live in `tests/cli/test_skill_install.py`; format tolerance in `tests/shared/test_frontmatter.py` and `tests/ava/test_skills.py`.

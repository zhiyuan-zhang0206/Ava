---
type: doc
title: ava-ui skill — Launch a web page for the user to view / collect replies
description: Give the agent frontend scaffolding to launch a page for the user — markdown+LaTeX / sync transcription display components, plus choice/confirm/form/compare interaction panels to send results back to the agent. Provides HTML/JS/TSX scaffolding, no Python.
tags:
- extensions
- agent-instruction
---

# ava-ui skill — Launch a web page for the user to view / collect replies

## What is it
Provides the agent with **frontend scaffolding** to launch a page for the user (`ava_builtins/skills/ava-ui/`). The key tradeoff it embodies: **no Python, no Python API** — it's all HTML/JS/TSX/starter projects; the agent uses file tools `read`/`cp` to fetch templates, assemble and edit them, then start a server. This way the agent can generate arbitrary rich pages without the framework having to carry an SDK function for every display form.

## Entry Points
- `ava.ui.serve(dir, name, port)` is a one-step static file path (start server + poll + register page); it's a **static file server**, `.md` files will be opened as **raw markdown source** (garbled) — so **never directly serve `.md`**, first run `ava.ui.serve_markdown()` or render to HTML with a markdown widget.
- Interaction panels (choice/confirm/form/compare) relay the user's choice back to the agent — "ask the user via a page," not just "display."

## Sub-skills — the design chain
`ava-ui` is a **root skill**: it carries two nested design sub-skills, and its own SKILL.md opens with a soft pointer at them (advisory, not mandatory — a throwaway render needs neither).
- `ava.skills.ava_ui.design` — page-level visual quality bar: how much design a request warrants, typography, neutrals, both themes, layout, copy.
- `ava.skills.ava_ui.dataviz` — charts/dashboards: form heuristic, color formula, a runnable palette validator (`scripts/validate_palette.py`).

Both are **vendored** from Claude Code bundled skills, not authored here; provenance and the adaptation list live in [VENDORED.md](../VENDORED.md). Nesting is deliberate: the system-prompt capability index is a whitelist that already contains `ava-ui`, so chaining off it is the reliable recall path for content that would otherwise need a config change on every cluster to be seen.

## Key Dependencies
- [[ava_builtins/skills/comms/comms.ava.okf.md|Communication & User Interaction Skills]] — parent functional group
- [[ava/ui.ava.okf.md|ava.ui]] — `serve` / `serve_markdown` / `notify` / `show` / `close` SDK itself
- [[ava_builtins/plugins/ava_fleet/notify.ava.okf.md|Notify]] — queue semantics of `ava.ui.notify` (human-side channel)

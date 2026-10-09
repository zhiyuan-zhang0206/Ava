---
type: doc
title: sweeper skill — Tech debt sweep engine
description: "A repo-agnostic sweep engine — performs a reconcile on a repo's \"current debt\" tracker: recheck open items, discover new debt, land a PR. It defines *how* to sweep, **not** *what* to sweep — the latter is supplied by project documentation or the caller (debt types + tracker path)."
tags:
- extensions
- agent-instruction
---

# sweeper skill — Tech debt sweep engine

## What it is
A **repo-agnostic process** for maintaining a repo's "current open debt" tracker (`$AVA_HOME/skills/sweeper/`). It defines *how* to sweep, **not** *what* to sweep. The caller or project documentation supplies the tracker path and authorized inspection classes. A separate project skill is not required. Missing inputs require clarification; ordinary contributions do not require a reconcile pass.

## Discipline of a single reconcile
- Each invocation = **one reconcile pass**, all changes land as a **PR, never push to main**.
- Recheck each `open` item's evidence whether it still holds; if not, delete (resolved); skip `wontfix` entirely, never re-evaluate.
- Inspect each authorized debt type to discover new debt, deduplicate by fingerprint, only add truly new ones, **never re-add wontfix**.
- Open a PR only if the tracker changes; if nothing changed, report no-op, no PR. The mechanical snapshot list (added N / resolved M) stays in the PR body, not in the tracker.

## Key dependencies
- [[ava_builtins/skills/docs/skills.ava.okf.md|Skills index]] — full skills catalog
- [[ava/skills/docs/skills.ava.okf.md|Skill System]] — two-level combination of engine + project-local skill supply
- [[scripts/docs/scripts.ava.okf.md|Ops scripts]] — division of labor with lint: see repo's lint-vs-sweeper (lint blocks mechanical, sweeper tracks semantic debt)

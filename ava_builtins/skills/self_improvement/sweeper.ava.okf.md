---
type: doc
title: sweeper skill — Tech debt sweep engine
description: "A repo-agnostic sweep engine — performs a reconcile on a repo's \"current debt\" tracker: recheck open items, discover new debt, land a PR. It defines *how* to sweep, **not** *what* to sweep — the latter is supplied by a project-local sweeper skill in the scanned repo (debt types + tracker path)."
tags:
- extensions
- agent-instruction
---

# sweeper skill — Tech debt sweep engine

## What it is
A **repo-agnostic process** for maintaining a repo's "current open debt" tracker (`$AVA_HOME/skills/sweeper/`). It defines *how* to sweep, **not** *what* to sweep. The key design split: the scanned repo carries a project-local sweeper skill (e.g., `ava.skills.sweeper_<repo>`) that supplies two things — ① the tracker file path (that single "current open debt" living document) ② debt types (specific commands / scans to uncover debt in that codebase). **Read that project-local skill first**; if the repo doesn't have one, say "scan not set up" instead of making up debt types.

## Discipline of a single reconcile
- Each invocation = **one reconcile pass**, all changes land as a **PR, never push to main**.
- Recheck each `open` item's evidence whether it still holds; if not, delete (resolved); skip `wontfix` entirely, never re-evaluate.
- Run each debt type to discover new debt, deduplicate by fingerprint, only add truly new ones, **never re-add wontfix**.
- Open a PR only if the tracker changes; if nothing changed, report no-op, no PR. The mechanical snapshot list (added N / resolved M) stays in the PR body, not in the tracker.

## Key dependencies
- [[ava_builtins/skills/self_improvement/self_improvement.ava.okf.md|Self-improvement & evolution skill]] — belongs to functional group
- [[ava/skills.ava.okf.md|Skill System]] — two-level combination of engine + project-local skill supply
- [[../../../scripts/scripts.ava.okf.md|Ops scripts]] — division of labor with lint: see repo's lint-vs-sweeper (lint blocks mechanical, sweeper tracks semantic debt)

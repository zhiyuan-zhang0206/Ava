---
type: doc
title: AGENTS.md size ceiling
description: The AGENTS.md line-count tiers and why its thresholds move only deliberately.
tags: [scripts, lint]
---

# AGENTS.md size ceiling

AGENTS.md is the entry point injected into every agent's context. The community
best practice (OpenAI / Anthropic Agent Skills, the AGENTS.md v1.1 proposal)
converges on a root file of about 100 lines — light enough to stay in context
permanently. Meiva's file was reduced from 385 to 81 lines (2026-07-14) by
extracting specialized rules into `docs/conventions/` under progressive
disclosure; `agents_md_size.py` keeps it from creeping back up.

AGENTS.md is measured in lines against two repo-wide constants: at or below the transitional floor the lint prints nothing and exits 0; past the floor and up to the hard ceiling it prints one non-blocking note and exits 0; past the hard ceiling it prints one error and exits 1. No per-line or per-file exemption exists, and the constants move only as a deliberate change in the same PR, justified in the commit message.

The transitional zone is the reminder to trim before the wall; the hard ceiling
fails the commit, so the entry point cannot grow back by accident. The two
constants are the source of truth and this page does not restate their values,
so a deliberate bump cannot leave the prose lying.

Parent: [[scripts/lint/docs/lint.meiva.okf.md]].

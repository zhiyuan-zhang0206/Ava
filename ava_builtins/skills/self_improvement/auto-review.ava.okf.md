---
type: doc
title: auto-review skill — PR semantic review
description: Ava's automated code reviewer — reviews a PR, posts a review comment. Its value lies in semantic judgments that CI cannot mechanize (AGENTS.md compliance, PR description quality, doc sync, security, test judgment, architectural consistency); absolutely never repeats mechanical rules already caught by CI.
tags:
- extensions
- agent-instruction
---

# auto-review skill — PR semantic review

## What it is
Ava's code reviewer (`$AVA_HOME/skills/auto-review/`): reviews **one PR**, posts a review comment. Its reason for existence is division of labor — CI (ruff/pyright/pytest/eslint/tsc/vitest/migration lint/structure lint) already covers every mechanical rule 100%, so the value of this skill is only in **semantic judgments that CI cannot catch**, and **absolutely never repeats things already caught by CI**.

## Six review dimensions
Go through six dimensions, judge each as pass / partial-concern, each finding must include **file path + line number** (no location, no finding): ① AGENTS.md compliance (highest value — natural language, unmechanizable; includes doc sync discipline: if API endpoints / schema fields / glossary / roadmap items are changed, are docs updated in the same PR) ② PR description quality ③ doc sync ④ security patterns ⑤ test coverage judgment ⑥ architectural consistency. When a 🔴 must-fix item is found, additionally escalate via `ava.ui.notify` (P1).

## Key dependencies
- [[ava_builtins/skills/self_improvement/self_improvement.ava.okf.md|Self-improvement & evolution skill]] — belongs to functional group
- [[ava_builtins/plugins/ava_fleet/notify.ava.okf.md|Notify]] — 🔴 items escalate via `ava.ui.notify`
- [[ava_builtins/plugins/ava_code/ava_code.ava.okf.md|AvaCode]] — same code workflow as PR / conventions / testing skills

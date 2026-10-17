---
type: doc
title: Text length budgets
description: CJK-aware text measurement and the shared soft/hard length-budget verdict.
tags: [base]
---

# Text length budgets

`text_budget.py` is the shared measure behind the repository's text-length
limits — memory notes and indexes, OKF nodes, AGENTS.md and SKILL.md, skill
descriptions, the `to_user` panel and inherited memory blocks. It is a leaf
module: the standard library only, so pre-commit lints, standalone scripts and
agent plugins can all import it cheaply.

The limits themselves stay with the site that enforces them: a marked constant
in a lint module with its reason, or a runtime setting resolved at call time.
This module owns only how text is measured and how one measurement compares
against one budget; the site decides what an over-limit verdict does (block a
commit, add a reminder note, truncate with a marker, reject the input).

## Units and counting

The budget chooses its unit by what it protects. `words` sizes prose that a
human reads (panels, skill descriptions); `chars` bounds stored or injected
text (memory notes and inherited blocks); `lines` bounds structured documents
(AGENTS.md, SKILL.md, memory indexes).

Text is measured in three units: chars is the code-point length; lines is the count of splitlines() pieces; words counts one unit per CJK character — kana, CJK extension A, unified ideographs, hangul, compatibility ideographs — plus one per maximal run of letters and digits, with underscore not continuing a run and CJK characters blanked first so runs never merge across scripts; punctuation, whitespace and other symbols count zero. An unknown unit raises.

A CJK character counts as one unit because one character carries about the
information an English word does: a flat character count would read a Chinese
page and an English sentence as the same size. `chars` counts code points
(`len`), not grapheme clusters or bytes.

## Budgets and verdicts

A length budget pairs one unit with a hard limit, a soft threshold, or both; 0 disables either limit and a negative limit is invalid; a set soft threshold must be strictly below a set hard limit. evaluate measures the budget's unit and returns ok at or below every set threshold, soft past the soft threshold while at or below the hard limit, and over past the hard limit, with over taking precedence. The verdict carries the measurement, the budget, and the remaining room under a set hard limit — None when no hard limit is set.

A soft threshold equal to the hard limit could never fire — `over` takes
precedence at the same measurement — so it is rejected as a dead setting
rather than stored. A site that expresses its warning band as remaining room
(`WARN_MARGIN`) sets `soft = hard - margin`; the numbers do not move. A site with only a reminder
threshold sets `soft` alone — an over-limit verdict cannot occur — and a site
that never blocks or truncates simply never acts on `over`. The disabled budget
(`hard=0, soft=0`) reports `ok` with `remaining=None`, so a site whose cap is
set to 0 uses one code path for both states.

## Threshold ownership

Thresholds are deliberately not registered here. Each site keeps its own
value with its reason: a runtime budget is a config field whose description
states why the default is what it is; a lint or CI budget is a marked
constant whose reason lives in the component's own documentation or decision
record. A new budget states its unit, its thresholds and its action at the
site rather than adding a second registry — one owner per fact.

Parent: [[base/packages/docs/packages.meiva.okf.md]].

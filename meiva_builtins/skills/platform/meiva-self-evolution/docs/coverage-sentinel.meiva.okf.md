---
type: doc
title: Self-evolution coverage headline and data-quality sentinel
description: How the daily scan states how much of a window it could judge, when a low-coverage window replaces the regression alert with a data-quality one, and where the floor comes from
tags:
- extensions
- agent-instruction
---

# Self-evolution coverage headline and data-quality sentinel

[[meiva_builtins/skills/platform/meiva-self-evolution/docs/evidence-block-and-grading.meiva.okf.md]]

A scan that cannot judge its window must not report it as healthy. The headline
states the share it could judge on every run, and a window below the floor alerts
as a data-quality problem instead of pretending either health or a regression.

## Where the floor comes from

`settings.general.self_evolution_coverage_alert_floor` (alias
`MEIVA_SELF_EVOLUTION_COVERAGE_ALERT_FLOOR`, cluster-pinned, no restart) defaults
to 0.5. It lives in the `general` domain rather than a `self_evolution` domain
because the scan runs as a subprocess of an agent process, so it inherits the
`MEIVA_PROCESS_PROFILE=agent` marker: a domain outside that profile raises
`profile_domain_error` at the read. Evidence grading leaves roughly a quarter of
current records unjudgeable, so 0.5 keeps that much headroom before a real
regression has to compete with missing telemetry.

## Report layer

`aggregate.py` compares only real measurements. A gather output whose `n` is 0
scored nothing, so its `mean` is the 0.0 that `_mean([])` invents — a placeholder
that reads as a terrible batch — and such a stamp yields no dimensions and never
serves as a baseline; `render_report` prints `n/a` for the overall delta. When a
gather output carries no `n` at all, a `None` mean is the fallback signal. An
earlier stamp of the same skill in that directory is otherwise the baseline.
`--total-runs` is required, so a report cannot claim a run count it was never
told.

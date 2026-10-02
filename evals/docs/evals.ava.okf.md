---
type: doc
title: "evals"
description: "Retired JSONL eval-case experiment; the weekly adversarial evaluation runs from schedules."
tags:
  - evaluation
---

# evals

The [schema-v1 JSONL format](../SCHEMA-v1.md), reference loader, and copied
adversarial dataset were never connected to a runner and have been retired.
The weekly adversarial evaluation still uses
[`schedules/adversarial_eval_cases.py`](../../schedules/adversarial_eval_cases.py)
through
[`schedules/adversarial-eval-weekly-schedule.py`](../../schedules/adversarial-eval-weekly-schedule.py).

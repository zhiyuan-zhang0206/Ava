---
type: doc
title: Self-evolution rubric abstention and coverage counts
description: When the rubric returns None instead of a number, how missing terms and dimensions normalize, and how evaluate.gather counts scored vs abstained samples
tags:
- extensions
- agent-instruction
---

# Self-evolution rubric abstention and coverage counts

[[meiva_builtins/skills/platform/meiva-self-evolution/docs/evidence-block-and-grading.meiva.okf.md]]

`None` means "this run cannot be judged", never 0 — a 0 would read as "very bad".
The evaluation loop compares means across runs, so an unjudgeable record must
leave the comparison instead of dragging it down.

```meiva-contract
id = "rubric-abstention"
kind = "invariant"
code = ["meiva_builtins/skills/platform/meiva-self-evolution/scripts/rubric.py:<module>"]
tests = [
  "meiva_builtins/skills/platform/meiva-self-evolution/tests/test_self_evolution_rubric.py::test_zero_telemetry_record_abstains",
  "meiva_builtins/skills/platform/meiva-self-evolution/tests/test_self_evolution_rubric.py::test_partial_grade_abstains",
  "meiva_builtins/skills/platform/meiva-self-evolution/tests/test_self_evolution_rubric.py::test_missing_terms_renormalize_completion",
  "meiva_builtins/skills/platform/meiva-self-evolution/tests/test_self_evolution_rubric.py::test_complete_record_with_exec_keeps_previous_scores",
  "meiva_builtins/skills/platform/meiva-self-evolution/tests/test_self_evolution_rubric.py::test_overall_is_none_when_efficiency_abstains",
  "meiva_builtins/skills/platform/meiva-self-evolution/tests/test_self_evolution_rubric.py::test_legacy_record_without_a_grade_keeps_scoring",
]
statement = """
`rubric.scores` returns three dimensions that are each a number or `None`, and `None` means unknown rather than zero. A record whose `evidence_grade` is present and not `complete` scores `None` on all three. A record without the key — every legacy one — scores exactly as before. Inside a dimension a term is missing rather than perfect when its denominator is empty: no exec events removes `exec_success` / `exec_clean`, zero turns removes `turn_thrift`, zero tokens removes `token_thrift`. The remaining terms keep their relative weights and are renormalized over the weights still available; `completion` abstains below two available terms, `efficiency` below three, and `overall` is `None` whenever either dimension is.
"""
```

## Term availability

| Dimension | Terms | A term is missing when |
| --- | --- | --- |
| completion | `has_output`, `exec_success`, `ended_clean` | no exec events (the rate has no denominator) |
| efficiency | `exec_clean`, `no_compaction`, `no_reprompt`, `turn_thrift`, `token_thrift` | no exec events, zero turns, zero tokens respectively |

`has_output`, `ended_clean`, `no_compaction` and `no_reprompt` are always
available. Normalization divides by the weights still present, so a record with
no exec events is no longer scored as if every execution succeeded: an empty
output with no exec score lands at `0.15 / 0.70`, not `0.45`.

## Dimension thresholds

`completion` needs two of its three terms and `efficiency` three of its five;
below that the dimension is `None`. A record with a known prompt that never
reached a terminal state has no exec events, no turns and no tokens, which leaves
efficiency two terms short and therefore abstains the whole record.

## Coverage counts

```meiva-contract
id = "coverage-counts"
kind = "invariant"
code = ["meiva_builtins/skills/platform/meiva-self-evolution/scripts/evaluate.py:<module>"]
tests = [
  "meiva_builtins/skills/platform/meiva-self-evolution/tests/test_self_evolution_rubric.py::test_score_summary_counts_scored_and_abstained",
  "meiva_builtins/skills/platform/meiva-self-evolution/tests/test_self_evolution_rubric.py::test_all_abstained_batch_has_no_mean",
]
statement = """
`evaluate.gather` reports `n_scored` and `n_abstained` beside `n` and `mean`. `mean` averages only samples whose `overall` is not `None`, and it is `None` per dimension when nothing scored, so a caller can print "nothing judged" instead of a zero. `verify_replay`, the `invalid` classification and the pending split are unchanged.
"""
```

An abstained mean reaches the report as an absence, not a number:
`aggregate.compute_score_dimensions` returns no dimension list when the mean has a
`None`, and `render_report.build_report_data` then prints `n/a` for the overall
delta instead of a fabricated `+0.000`. A `n/a` says the batch was not judged; a
zero would say it did not change.

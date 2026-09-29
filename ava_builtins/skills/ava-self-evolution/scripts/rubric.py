"""Rubric = the loss function of the evaluation loop.

Pure functions over one trace record (the shape `collect.py` produces). No LLM,
no DB. Two dimensions the loop optimizes a skill against:

    completion  did the task actually get done — output produced, no delivery
                breach, code executed cleanly, did not end on a failed exec.
    efficiency  was the process optimal — few tokens, few turns, no exec
                failures, no compaction pressure, no user re-prompts.

`overall` is a weighted composite (completion dominates: a cheap run that did
not finish is worthless). Every score is in [0, 1], higher is better.

These are proxy signals over observable trace data, not ground truth. The loop
reads them comparatively — the same rubric over a baseline run and a re-run
under an edited skill, so what matters is the delta, not the absolute value.
The cost terms (`turn_thrift`, `token_thrift`) saturate around a reference
budget that only sets the midpoint; tune the constants, the delta still holds.
"""

from __future__ import annotations

from typing import Any

COMPLETION_WEIGHTS = {
    "has_output": 0.4,
    "not_breached": 0.3,
    "exec_success": 0.2,
    "ended_clean": 0.1,
}
EFFICIENCY_WEIGHTS = {
    "exec_clean": 0.30,
    "no_compaction": 0.20,
    "no_reprompt": 0.20,
    "turn_thrift": 0.15,
    "token_thrift": 0.15,
}
OVERALL_WEIGHTS = {"completion": 0.7, "efficiency": 0.3}

# Cost midpoints — a run at exactly this many turns / tokens scores 0.5 on that
# thrift term; fewer scores higher, more scores lower. Rough by design.
REF_TURNS = 8.0
REF_TOKENS = 60_000.0


def _exec_success_rate(rec: dict[str, Any]) -> float:
    total = rec["exec_ok"] + rec["exec_failed"]
    return rec["exec_ok"] / total if total else 1.0


def _saturating(value: float, ref: float) -> float:
    """Map a non-negative cost to (0, 1]: ref/(ref+value). 1.0 at value=0,
    0.5 at value=ref, → 0 as value grows. Monotonic decreasing."""
    return ref / (ref + max(0.0, value))


def _weighted(terms: dict[str, float], weights: dict[str, float]) -> float:
    return round(sum(terms[k] * w for k, w in weights.items()), 3)


def completion(rec: dict[str, Any]) -> float:
    terms = {
        "has_output": 1.0 if rec["final_output"].strip() else 0.0,
        "not_breached": 0.0 if rec["breached"] else 1.0,
        "exec_success": _exec_success_rate(rec),
        "ended_clean": 0.0 if rec["last_exec_failed"] else 1.0,
    }
    return _weighted(terms, COMPLETION_WEIGHTS)


def efficiency(rec: dict[str, Any]) -> float:
    terms = {
        "exec_clean": _exec_success_rate(rec),
        "no_compaction": 1.0 / (1.0 + rec["compactions"]),
        "no_reprompt": 1.0 / (1.0 + len(rec["followup_prompts"])),
        "turn_thrift": _saturating(rec["turns"], REF_TURNS),
        "token_thrift": _saturating(rec["tokens_in"] + rec["tokens_out"], REF_TOKENS),
    }
    return _weighted(terms, EFFICIENCY_WEIGHTS)


def overall(rec: dict[str, Any]) -> float:
    return _weighted(
        {"completion": completion(rec), "efficiency": efficiency(rec)}, OVERALL_WEIGHTS
    )


def scores(rec: dict[str, Any]) -> dict[str, float]:
    """All three dimensions for one record: {completion, efficiency, overall}."""
    return {
        "completion": completion(rec),
        "efficiency": efficiency(rec),
        "overall": overall(rec),
    }

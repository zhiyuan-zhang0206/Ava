# meiva:ref meiva_builtins/skills/platform/meiva-self-evolution/docs/rubric-abstention.meiva.okf.md#rubric-abstention
from __future__ import annotations

from typing import Any

COMPLETION_WEIGHTS = {
    "has_output": 0.55,
    "exec_success": 0.30,
    "ended_clean": 0.15,
}
EFFICIENCY_WEIGHTS = {
    "exec_clean": 0.30,
    "no_compaction": 0.20,
    "no_reprompt": 0.20,
    "turn_thrift": 0.15,
    "token_thrift": 0.15,
}
OVERALL_WEIGHTS = {"completion": 0.7, "efficiency": 0.3}
COMPLETION_MIN_TERMS = 2
EFFICIENCY_MIN_TERMS = 3

# Cost midpoints — a run at exactly this many turns / tokens scores 0.5 on that
# thrift term; fewer scores higher, more scores lower. Rough by design.
REF_TURNS = 8.0
REF_TOKENS = 60_000.0


def _exec_success_rate(rec: dict[str, Any]) -> float | None:
    total = rec["exec_ok"] + rec["exec_failed"]
    return rec["exec_ok"] / total if total else None


def _saturating(value: float, ref: float) -> float:
    """Map a non-negative cost to (0, 1]: ref/(ref+value). 1.0 at value=0,
    0.5 at value=ref, → 0 as value grows. Monotonic decreasing."""
    return ref / (ref + max(0.0, value))


def _weighted(
    terms: dict[str, float | None], weights: dict[str, float], minimum: int
) -> float | None:
    available = {name: value for name, value in terms.items() if value is not None}
    if len(available) < minimum:
        return None
    total_weight = sum(weights[name] for name in available)
    return round(sum(value * weights[name] for name, value in available.items()) / total_weight, 3)


def completion(rec: dict[str, Any]) -> float | None:
    terms = {
        "has_output": 1.0 if rec["final_output"].strip() else 0.0,
        "exec_success": _exec_success_rate(rec),
        "ended_clean": 0.0 if rec["last_exec_failed"] else 1.0,
    }
    return _weighted(terms, COMPLETION_WEIGHTS, COMPLETION_MIN_TERMS)


def efficiency(rec: dict[str, Any]) -> float | None:
    tokens = rec["tokens_in"] + rec["tokens_out"]
    terms = {
        "exec_clean": _exec_success_rate(rec),
        "no_compaction": 1.0 / (1.0 + rec["compactions"]),
        "no_reprompt": 1.0 / (1.0 + len(rec["followup_prompts"])),
        "turn_thrift": _saturating(rec["turns"], REF_TURNS) if rec["turns"] else None,
        "token_thrift": _saturating(tokens, REF_TOKENS) if tokens else None,
    }
    return _weighted(terms, EFFICIENCY_WEIGHTS, EFFICIENCY_MIN_TERMS)


def overall(rec: dict[str, Any]) -> float | None:
    completed = completion(rec)
    efficient = efficiency(rec)
    if completed is None or efficient is None:
        return None
    return round(
        completed * OVERALL_WEIGHTS["completion"] + efficient * OVERALL_WEIGHTS["efficiency"], 3
    )


def scores(rec: dict[str, Any]) -> dict[str, float | None]:
    if rec.get("evidence_grade") not in ("complete", None):
        return {"completion": None, "efficiency": None, "overall": None}
    return {
        "completion": completion(rec),
        "efficiency": efficiency(rec),
        "overall": overall(rec),
    }

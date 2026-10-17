# meiva:ref meiva_builtins/skills/platform/meiva-self-evolution/docs/evidence-block-and-grading.meiva.okf.md#unknown-label
from __future__ import annotations

from typing import Any

LABELS = ("ok", "fumbled", "failed", "unknown")
REGRESSION_LABELS = ("failed", "fumbled")

# Thresholds — a run is "fumbled" once any single signal crosses these.
FUMBLE_MIN_REPROMPTS = 2
FUMBLE_MIN_COMPACTIONS = 3
FUMBLE_MIN_EXEC_FAILURES = 3
FUMBLE_TURNS_PER_EXEC_FAILURE = 10


def label(rec: dict[str, Any]) -> str:
    grade = rec.get("evidence_grade")
    if grade is None:
        return _rule_label(rec)
    if grade != "complete":
        return "unknown"
    return _rule_label(rec)


def _rule_label(rec: dict[str, Any]) -> str:
    if _is_failed(rec):
        return "failed"
    if _is_fumbled(rec):
        return "fumbled"
    return "ok"


def _delivered_via_side_channel(rec: dict[str, Any]) -> bool:
    """True when the agent delivered results through send_message, notify,
    serve, or a result file (files.write / files.append) — even though
    final_output may be empty.

    File delivery is the standard meiva-fleet worker pattern: the worker
    writes its result JSON and terminates without a report string. Without
    it, completed workers are mislabeled `failed` (2026-08-03 weekly run:
    9 of 25 failed runs were file-delivering workers)."""
    tc = rec["tools_called"]
    return (
        tc.get("meiva.agents.send_message", 0) > 0
        or tc.get("meiva.ui.notify", 0) > 0
        or tc.get("meiva.ui.serve", 0) > 0
        or tc.get("meiva.files.write", 0) > 0
        or tc.get("meiva.files.append", 0) > 0
    )


def _is_failed(rec: dict[str, Any]) -> bool:
    delivered = _delivered_via_side_channel(rec)
    # A 0-turn agent was spawned but never scheduled to run — not a real
    # failure of the agent itself.  Also exempt agents that delivered via
    # side channels or a result file (they completed their task, just
    # terminated without a report string).
    if rec["terminated"] and not rec["final_output"].strip() and rec["turns"] > 0 and not delivered:
        return True
    return bool(rec["last_exec_failed"])


def _is_fumbled(rec: dict[str, Any]) -> bool:
    # User re-prompts (neutral follow-ups) — 2+ = fumbled.
    if len(rec["followup_prompts"]) >= FUMBLE_MIN_REPROMPTS:
        return True
    # User corrections — even one is a stronger signal than re-prompts.
    # The user had to redirect/correct the agent; treat as fumbled.
    if len(rec.get("corrections", [])) > 0:
        return True
    # Peer feedback — another agent had to step in and correct.
    # Also a fumble signal: the agent needed external correction.
    if len(rec.get("peer_feedback", [])) > 0:
        return True
    # A few execution failures are normal exploratory work. Keep the
    # three-failure floor for short workers, then require one failure per ten
    # turns so a completed long-running worker is not penalized for iterations.
    exec_failure_threshold = max(
        FUMBLE_MIN_EXEC_FAILURES,
        -(-rec["turns"] // FUMBLE_TURNS_PER_EXEC_FAILURE),
    )
    if rec["exec_failed"] >= exec_failure_threshold:
        return True
    return rec["compactions"] >= FUMBLE_MIN_COMPACTIONS

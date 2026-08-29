"""Rule-based outcome label for one collected agent-run trace record.

Pure function, no LLM and no DB. `label(rec)` reads only fields that
`collect.py` assembles onto every record and returns one of three tiers:

    failed   the run visibly broke: it breached a delivery obligation, it
             terminated without producing any output, or its last code
             execution failed and never recovered.
    fumbled  the run got there but messily: the user had to correct or
             re-prompt two or more times, another agent had to step in with
             corrective feedback, some code execution failed mid-run, or the
             context was compacted repeatedly.
    ok       none of the above negative signals fired.

`ok` means "no objective failure signal", NOT "high quality" — the absolute
quality of an organic run cannot be measured without ground truth, so this
labeler deliberately only detects visible breakage. `failed` / `fumbled` are
the records worth mining for skill/plugin regressions.

The record fields consumed here are a hard contract with collect.py; they are
indexed directly (not `.get`) so a missing field fails fast rather than
silently mislabeling.
"""

from __future__ import annotations

from typing import Any

LABELS = ("ok", "fumbled", "failed")

# Thresholds — a run is "fumbled" once any single signal crosses these.
FUMBLE_MIN_REPROMPTS = 2
FUMBLE_MIN_COMPACTIONS = 3
FUMBLE_MIN_EXEC_FAILURES = 3
FUMBLE_TURNS_PER_EXEC_FAILURE = 10


def label(rec: dict[str, Any]) -> str:
    """Classify one trace record into ok / fumbled / failed."""
    if _is_failed(rec):
        return "failed"
    if _is_fumbled(rec):
        return "fumbled"
    return "ok"


def _delivered_via_side_channel(rec: dict[str, Any]) -> bool:
    """True when the agent delivered results through send_message, notify,
    serve, or a result file (files.write / files.append) — even though
    final_output may be empty.

    File delivery is the standard ava-fleet worker pattern: the worker
    writes its result JSON and terminates without a report string. Without
    it, completed workers are mislabeled `failed` (2026-08-03 weekly run:
    9 of 25 failed runs were file-delivering workers)."""
    tc = rec["tools_called"]
    return (
        tc.get("ava.agents.send_message", 0) > 0
        or tc.get("ava.ui.notify", 0) > 0
        or tc.get("ava.ui.serve", 0) > 0
        or tc.get("ava.files.write", 0) > 0
        or tc.get("ava.files.append", 0) > 0
    )


def _is_failed(rec: dict[str, Any]) -> bool:
    # Context breach alone is not failure when the agent delivered results
    # through side channels or a result file — the breach is a fumble
    # signal, not a crash.
    delivered = _delivered_via_side_channel(rec)
    if rec["breached"] and not delivered:
        return True
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
    # Context breach with side-channel delivery — the agent did deliver
    # but its context overflowed; this is a fumble, not a full failure.
    if rec["breached"]:
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

"""Unit tests for the F12b/restart sampling helpers in scripts/two_section_chain_smoke.py.

The smoke itself needs launchd, a compiled helper, and an unlocked desktop
session, so CI cannot run it; these tests pin the pure parse/join/summary/
assert helpers with synthetic fixtures instead (F12b, task #3380).
"""

from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "two_section_chain_smoke.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("two_section_chain_smoke", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke = _load_script()


def _line(stamp: str, requesting_pid: int, responsible_pid: int | None = 400) -> str:
    responsible = (
        f"responsible={{TCCDProcess: identifier=com.ava.helper, pid={responsible_pid}, auid=501}}, "
        if responsible_pid is not None
        else ""
    )
    return (
        f"{stamp} 0x1234 Default tccd[99]: AUTHREQ_ATTRIBUTION: {responsible}"
        f"requesting={{TCCDProcess: identifier=com.ava.smoke, pid={requesting_pid}, auid=501}}"
    )


def test_parse_window_indexes_by_requesting_pid() -> None:
    good = _line("2026-09-14 15:30:01.123", 5001)
    no_responsible = _line("2026-09-14 15:30:01.900", 5002, responsible_pid=None)
    garbage = "not a log line requesting={TCCDProcess: identifier=x, pid=5003}"
    no_requesting = "2026-09-14 15:30:02.000 0x1 Default tccd: something else entirely"
    window = smoke._f12_parse_window("\n".join([good, no_responsible, garbage, no_requesting]))
    assert set(window) == {5001, 5002}
    ts, line = window[5001][0]
    assert line == good
    expected = datetime.datetime(2026, 9, 14, 15, 30, 1, 123000).astimezone().timestamp()
    assert abs(ts - expected) < 0.001
    assert window[5002][0][1] == no_responsible


def test_join_filters_window_and_caps_at_three() -> None:
    ts = 1000.0
    rows = [
        {"round": "pre", "pid": 5001, "unit": "heartbeat", "ts": ts},
        {"round": "pre", "pid": 5002, "unit": "heartbeat-b", "error": "unit dead"},
    ]
    samples = {"points": {"pre": {"smoke_ts": ts, "rows": rows}}}
    lines = {
        5001: [
            (ts - 0.20, _line("s", 5001)),  # outside the 0.15s lookback window
            (ts - 0.10, _line("s", 5001, responsible_pid=None)),  # kept; no responsible pid
            (ts, _line("s", 5001)),
            (ts + 0.01, _line("s", 5001)),
            (ts + 0.02, _line("s", 5001)),  # dropped: the join keeps the first 3
        ]
    }
    joined = smoke._f12_join(samples, lines)
    assert [row["pid"] for row in joined] == [5001]
    requests = joined[0]["requests"]
    assert [request["line_ts"] for request in requests] == [ts - 0.10, ts, ts + 0.01]
    assert requests[0]["responsible_pid"] is None
    assert requests[1]["responsible_pid"] == 400


def test_attribution_summary_counts() -> None:
    samples = {
        "points": {
            "h0": {
                "rows": [
                    {"round": "h0", "pid": 1, "unit": "heartbeat", "ts": 10.0},
                    {"round": "h0", "pid": 2, "unit": "heartbeat-b", "error": "unit dead"},
                ]
            },
            "h1": {
                "rows": [{"round": "h1", "pid": 2, "unit": "heartbeat-b", "error": "no response"}]
            },
        },
        "tccd": [
            {
                "unit": "heartbeat",
                "point": "h0",
                "pid": 1,
                "requests": [{"responsible_pid": 400}, {"responsible_pid": None}],
            },
            {
                "unit": "heartbeat-b",
                "point": "h1",
                "pid": 2,
                "requests": [{"responsible_pid": 400}],
            },
        ],
    }
    summary = smoke._f12_attribution_summary(samples)
    assert summary["by_point"]["h0"] == {
        "requests": 2,
        "attributed": 1,
        "unattributed": 1,
        "missing_rounds": 1,
    }
    assert summary["by_point"]["h1"] == {
        "requests": 1,
        "attributed": 1,
        "unattributed": 0,
        "missing_rounds": 1,
    }
    assert summary["totals"] == {
        "requests": 3,
        "attributed": 2,
        "unattributed": 1,
        "missing_rounds": 2,
    }


def test_expect_attribution_violations() -> None:
    samples = {
        "tccd": [
            {"unit": "heartbeat", "point": "h0", "pid": 1, "requests": [{"responsible_pid": 400}]},
            {
                "unit": "heartbeat-b",
                "point": "h0",
                "pid": 2,
                "requests": [{"responsible_pid": None}],
            },
            {"unit": "heartbeat", "point": "h4", "pid": 7, "requests": [{"responsible_pid": 401}]},
        ]
    }
    # All rows at h0 must resolve to 400; a missing responsible pid is a violation.
    assert smoke._f12_expect_attribution(samples, [("h0", None, 400)]) == [
        "h0 heartbeat-b pid 2: request has no responsible"
    ]
    # Mismatch against the expected responsible pid.
    assert smoke._f12_expect_attribution(samples, [("h4", [7], 400)]) == [
        "h4 heartbeat pid 7: responsible pid 401 != expected 400"
    ]
    # Matching expectation passes; points absent from the expectations are never checked.
    assert smoke._f12_expect_attribution(samples, [("h4", [7], 401)]) == []
    # A listed pid with no joined row (dead or silent) is a violation.
    assert smoke._f12_expect_attribution(samples, [("h4", [7, 8], 401)]) == [
        "h4: no sampled row for pid 8 (dead or no response)"
    ]
    # A joined row without requests fails loudly too.
    empty = {"unit": "u", "point": "h4", "pid": 7, "requests": []}
    assert smoke._f12_expect_attribution({"tccd": [empty]}, [("h4", [7], 400)]) == [
        "h4 u pid 7: no requests observed"
    ]
    assert smoke._f12_expect_attribution({"tccd": []}, [("h0", None, 400)]) == [
        "h0: no sampled rows"
    ]

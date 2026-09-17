"""Lock the weekly schedule's /api/events count contract.

2026-08-18 change: meta.total is opt-in behind with_total=1. count_events is
the one consumer that genuinely needs the count (weekly volume threshold), so
it must request with_total=1 and raise loudly if the total is still absent —
never int(None) crash, never a silent 0 that skips the week's deep run.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

SCHED = Path(__file__).resolve().parents[2] / "schedules" / "self-evolution-weekly-schedule.py"


@pytest.fixture(scope="module")
def weekly_mod() -> Any:
    """Load the schedule script as a module — guarded by __main__, so import
    does not enter the sleep loop."""
    spec = importlib.util.spec_from_file_location("self_ev_weekly", SCHED)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["self_ev_weekly"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Resp:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


def _fake_get(payload: dict[str, object], calls: list[dict[str, object]]) -> Any:
    def get(
        url: str,
        *,
        params: dict[str, object] | None = None,
        headers: dict[str, object] | None = None,
        timeout: object = None,
    ) -> _Resp:
        calls.append(dict(params or {}))
        return _Resp(payload)

    return get


def test_count_events_requests_with_total(weekly_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(httpx, "get", _fake_get({"meta": {"total": 42}}, calls))

    assert weekly_mod.count_events(datetime.now(UTC)) == 42
    assert calls and calls[0]["with_total"] == 1


def test_count_events_raises_when_total_missing(
    weekly_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(httpx, "get", _fake_get({"meta": {}, "items": []}, calls))

    with pytest.raises(RuntimeError, match="no total"):
        weekly_mod.count_events(datetime.now(UTC))


# ── no-observability fallback (2026-09-17) ──────────────────────────────────


class _StatusResp:
    """httpx.Response-alike: a status + a parsed problem body. The refusal
    path must check the body without ever calling raise_for_status."""

    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        raise AssertionError("the refusal path must not raise_for_status")

    def json(self) -> object:
        return self._payload


def test_count_events_falls_back_to_mirror_on_no_observability_refusal(
    weekly_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no-observability cluster refuses /api/events reads (503
    observability_read_unavailable — a policy state): the weekly trigger
    counts the window from the local mirror instead of raising, so the
    schedule's fire path cannot crash-loop there."""
    refusal = _StatusResp(
        503,
        {
            "code": "observability_read_unavailable",
            "status": 503,
            "detail": "observability reads unavailable for this cluster",
        },
    )
    calls: list[dict[str, object]] = []

    def get(
        url: str,
        *,
        params: dict[str, object] | None = None,
        headers: dict[str, object] | None = None,
        timeout: object = None,
    ) -> Any:
        calls.append(dict(params or {}))
        return refusal

    monkeypatch.setattr(httpx, "get", get)

    def _count_stub(_since: datetime) -> int:
        return 12345

    monkeypatch.setattr(weekly_mod, "_count_mirror_events", _count_stub)

    assert weekly_mod.count_events(datetime.now(UTC)) == 12345
    assert calls and calls[0]["with_total"] == 1  # the request shape is unchanged


def test_count_mirror_events_filters_the_window_by_ts(
    weekly_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mirror files partition rows by append day, not by ts, so a boundary
    day's file holds rows from both sides of the window: the counter must
    filter each line's ts to [since, now]. Missing day files count as zero."""
    logs = tmp_path / "logs"
    logs.mkdir()
    now = datetime.now(UTC)
    since = now - timedelta(days=1)
    in_window = (since + timedelta(hours=1)).isoformat()
    before_window = (since - timedelta(hours=1)).isoformat()
    after_window = (now + timedelta(minutes=5)).isoformat()

    def row(ts: str) -> dict[str, object]:
        return {"ts": ts, "category": "telemetry", "event_name": "turn_end"}

    (logs / f"events-{since:%Y%m%d}.jsonl").write_text(
        json.dumps(row(in_window)) + "\n" + json.dumps(row(before_window)) + "\n"
    )
    (logs / f"events-{now:%Y%m%d}.jsonl").write_text(
        json.dumps(row(in_window)) + "\n" + json.dumps(row(after_window)) + "\n"
    )
    monkeypatch.setattr("shared.paths.logs_dir", lambda: logs)

    assert weekly_mod._count_mirror_events(since) == 2  # in_window twice; edges dropped

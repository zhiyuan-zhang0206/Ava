"""Lock the weekly schedule's /api/events count contract.

2026-08-18 change: meta.total is opt-in behind with_total=1. count_events is
the one consumer that genuinely needs the count (weekly volume threshold), so
it must request with_total=1 and raise loudly if the total is still absent —
never int(None) crash, never a silent 0 that skips the week's deep run.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
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

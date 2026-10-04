"""`report_plugin_load_failure` reports loudly without raising (task #4979)."""

from __future__ import annotations

from typing import Any

import pytest

from base.packages.plugins.load_report import report_plugin_load_failure


def test_load_failure_report_carries_the_traceback(
    loguru_records: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fail-soft report is one loguru ERROR carrying the traceback — the
    telemetry half may fail, the log half must not lose the cause (task #4979)."""

    def _broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("telemetry down")

    monkeypatch.setattr("base.telemetry.emit", _broken)
    report_plugin_load_failure("demo", RuntimeError("boom"))

    record = next(r for r in loguru_records if "failed to load" in r["message"])
    assert record["exception"] is not None
    assert record["exception"].type is RuntimeError

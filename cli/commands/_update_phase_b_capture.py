"""Best-effort host telemetry capture for the Phase-B poll."""

from __future__ import annotations

from typing import cast


def capture_host_stages(
    host_outcomes: dict[str, dict[str, object]] | None,
    name: str,
    result: object,
) -> None:
    """Copy updater stages, terminal wall time, and outcome from one probe."""
    if host_outcomes is None or not isinstance(result, dict):
        return
    raw: object | None = cast(dict[str, object], result).get("last_updater_outcome")
    if not isinstance(raw, dict):
        return
    outcome = cast(dict[str, object], raw)
    stages: object | None = outcome.get("stages")
    parsed: dict[str, object] = {}
    if isinstance(stages, dict):
        parsed = {
            str(key): round(float(value), 1)
            for key, value in cast(dict[str, object], stages).items()
            if isinstance(value, (int, float))
        }
    total_s = outcome.get("total_s")
    if isinstance(total_s, (int, float)):
        parsed["total_s"] = round(float(total_s), 1)
    kind = outcome.get("kind")
    rc = outcome.get("rc")
    if isinstance(kind, str):
        terminal: dict[str, object] = {"kind": kind}
        if isinstance(rc, int):
            terminal["rc"] = rc
        parsed["outcome"] = terminal
    if parsed:
        host_outcomes[name] = parsed

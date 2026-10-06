"""SDK expansion recommendations use runtime evidence and explicit windows."""

import json
from pathlib import Path

import pytest

from scripts.audit.sdk_usage import CallEvent, main, read_calls, report, timestamp


def event(identity: int, method: str, agent: int = 1, weight: int = 1) -> CallEvent:
    return CallEvent.model_validate(
        {
            "id": identity,
            "ts": "2026-10-06T00:00:00Z",
            "agent_id": agent,
            "attributes": {"fn": method, "sample_rate": weight},
        }
    )


START = timestamp("2026-10-06T00:00:00Z")
END = timestamp("2026-10-07T00:00:00Z")


def test_cumulative_selection_weights_calls_and_keeps_whole_modules() -> None:
    events = [
        event(1, "ava.files.read", weight=5),
        event(2, "shell.run", 2, 3),
        event(3, "files.read", 2),
        event(4, "agents.spawn"),
        event(5, "skills.read", weight=100),
        event(6, "help", weight=100),
    ]
    data = report(events, START, END)
    assert data["selected_sdk_modules"] == ["files", "shell"]
    assert data["eligible_module_calls"] == 10
    assert data["weighted_calls"] == 210
    files = next(row for row in data["modules"] if row["module"] == "files")
    assert files["observed_agents"] == 2
    assert data["sampled_events"] == 4


def test_window_and_agent_filters_apply_before_ranking() -> None:
    events = [event(1, "files.read", 1), event(2, "shell.run", 2)]
    assert report(events, START, END, agent_ids={2})["selected_sdk_modules"] == ["shell"]
    empty = report(events, END, END)
    assert empty["suggested_ava_sdk_expand"] is None
    assert empty["observed_events"] == 0


def test_mirrors_deduplicate_and_reject_conflicting_event_ids(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    raw = {**event(1, "files.read").model_dump(mode="json"), "event_name": "sdk_call"}
    path.write_text(json.dumps(raw) + "\n" + json.dumps(raw) + "\n")
    assert len(read_calls([path, path])) == 1
    raw["attributes"] = {"fn": "shell.run", "sample_rate": 1}
    with path.open("a") as stream:
        stream.write(json.dumps(raw) + "\n")
    with pytest.raises(ValueError, match=r"events\.jsonl:3: conflicting"):
        read_calls([path])


@pytest.mark.parametrize("weight", [0, -1, True, "5"])
def test_invalid_sample_weight_fails_at_boundary(weight: object) -> None:
    with pytest.raises(ValueError):
        event(1, "files.read", weight=weight)  # type: ignore[arg-type]


def test_invalid_window_and_coverage_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        timestamp("2026-10-06")
    with pytest.raises(ValueError, match="start <= end"):
        report([], END, START)
    for coverage in [0, 101, float("nan")]:
        with pytest.raises(ValueError, match="coverage"):
            report([], START, END, coverage)


def test_cli_reports_malformed_input_with_location(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"event_name":"sdk_call"}\n')
    with pytest.raises(SystemExit) as exc:
        main([str(path), "--start", START.isoformat(), "--end", END.isoformat()])
    assert exc.value.code == 2

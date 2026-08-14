from __future__ import annotations

import importlib

import pytest

_SENTINEL = "<!-- lint:roster-table -->"


def _roster_table(*services: str) -> str:
    """Build a synthetic runbook fragment: a sentinel + a roster table whose
    first column is a `` `service` `` code span per row. Includes the
    `agent-{N}` non-service row to exercise the _NON_SERVICE_ROWS exemption."""
    rows = [
        "| `agent-{N}` | one session per agent | restarter |",
    ]
    rows += [f"| `{name}` | runs {name} | healthcheck |" for name in services]
    return (
        "Some preamble prose.\n\n"
        f"{_SENTINEL}\n"
        "| Service (suffix) | Runs | Healthcheck |\n"
        "|------------------|------|-------------|\n"
        + "\n".join(rows)
        + "\n\nTrailing prose after the table.\n"
    )


def _load_lint(monkeypatch, registered, runbook_text, tmp_path):
    """Load the lint module with build_services() faked to `registered` and the
    runbook pointed at a temp file containing `runbook_text`."""
    lint = importlib.import_module("scripts.lint_doc_roster")

    class _FakeSpec:
        def __init__(self, session: str) -> None:
            self.session = session

    monkeypatch.setattr(lint, "build_services", lambda: tuple(_FakeSpec(s) for s in registered))
    runbook = tmp_path / "runbook.md"
    runbook.write_text(runbook_text, encoding="utf-8")
    monkeypatch.setattr(lint, "_RUNBOOK", runbook)
    return lint


def test_roster_matches_registered_passes(monkeypatch, tmp_path):
    services = ["gateway", "labeler", "gateway-watchdog"]
    lint = _load_lint(monkeypatch, services, _roster_table(*services), tmp_path)
    assert lint.check() == 0


def test_roster_has_extra_service_fails(monkeypatch, tmp_path):
    registered = ["gateway", "labeler"]
    # The roster documents `scheduler` on top of the registered set (the #728 case).
    text = _roster_table("gateway", "labeler", "scheduler")
    lint = _load_lint(monkeypatch, registered, text, tmp_path)
    assert lint.check() == 1


def test_registered_missing_from_roster_fails(monkeypatch, tmp_path):
    # build_services() registers `browser` but the roster forgot to document it.
    registered = ["gateway", "labeler", "browser"]
    text = _roster_table("gateway", "labeler")
    lint = _load_lint(monkeypatch, registered, text, tmp_path)
    assert lint.check() == 1


def test_missing_sentinel_errors(monkeypatch, tmp_path):
    registered = ["gateway", "labeler"]
    # A roster table with no sentinel above it.
    text = (
        "| Service (suffix) | Runs | Healthcheck |\n"
        "|------------------|------|-------------|\n"
        "| `gateway` | runs gateway | healthcheck |\n"
        "| `labeler` | runs labeler | healthcheck |\n"
    )
    lint = _load_lint(monkeypatch, registered, text, tmp_path)
    assert lint.check() == 1


def test_parse_roster_raises_without_sentinel():
    lint = importlib.import_module("scripts.lint_doc_roster")
    with pytest.raises(lint.RosterSentinelMissingError):
        lint.parse_roster("no sentinel here\n| `gateway` | x | y |\n")


def test_parse_roster_extracts_first_column_only():
    lint = importlib.import_module("scripts.lint_doc_roster")
    text = _roster_table("gateway", "labeler")
    parsed = lint.parse_roster(text)
    # First column only: code spans in later columns must not leak in.
    assert parsed == {"agent-{N}", "gateway", "labeler"}

from __future__ import annotations

import importlib

import pytest

_SENTINEL = "<!-- lint:roster-table -->"


def _roster_table(*services: str) -> str:
    """Build a synthetic service roster with the required sentinel."""
    rows = [f"| `{name}` | runs {name} | healthcheck |" for name in services]
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
    assert parsed == {"gateway", "labeler"}


# ─── healthcheck roster (issue #192) ────────────────────────────────────────

_HEALTHCHECK_SENTINEL = "<!-- lint:healthcheck-roster-table -->"


def _healthcheck_table(*modules: str) -> str:
    """A synthetic healthcheck roster fragment: sentinel + table whose first
    column is a `module.py` code span per row."""
    rows = [f"| `{name}.py` | service | probe | restart | certifies |" for name in modules]
    return (
        "Some preamble prose.\n\n"
        f"{_HEALTHCHECK_SENTINEL}\n"
        "| Module | Service | Probe | Restart | What it certifies |\n"
        "|--------|---------|-------|---------|-------------------|\n"
        + "\n".join(rows)
        + "\n\nTrailing prose after the table.\n"
    )


class _FakeSpecWithHealthcheck:
    def __init__(self, session: str, healthcheck_module: str | None) -> None:
        self.session = session
        self.healthcheck_module = healthcheck_module


def _load_healthcheck_lint(
    monkeypatch,
    *,
    spec_modules: set[str],
    directory: set[str],
    hand_added: set[str],
    table_text: str,
    tmp_path,
):
    """Load the lint with all three healthcheck-roster sources faked and the
    roster doc pointed at a temp file."""
    lint = importlib.import_module("scripts.lint_doc_roster")

    def fake_services():
        return tuple(
            _FakeSpecWithHealthcheck(f"session-{m}", f"services.healthchecks.{m}")
            for m in spec_modules
        )

    monkeypatch.setattr(lint, "build_services", fake_services)
    monkeypatch.setattr(lint, "directory_healthchecks", lambda: directory)
    monkeypatch.setattr(lint, "hand_added_healthchecks", lambda: hand_added)
    roster = tmp_path / "check-roster.ava.okf.md"
    roster.write_text(table_text, encoding="utf-8")
    monkeypatch.setattr(lint, "_HEALTHCHECK_ROSTER", roster)
    return lint


def test_healthcheck_roster_matches_passes(monkeypatch, tmp_path):
    lint = _load_healthcheck_lint(
        monkeypatch,
        spec_modules={"gateway", "browser"},
        directory={"gateway", "browser", "lgtm"},
        hand_added={"lgtm"},
        table_text=_healthcheck_table("gateway", "browser", "lgtm"),
        tmp_path=tmp_path,
    )
    assert lint.check_healthcheck_roster() == 0


def test_healthcheck_roster_phantom_row_fails(monkeypatch, tmp_path):
    """The audit's drift case: the table documents a module that no longer
    exists in the directory (task_maintenance)."""
    lint = _load_healthcheck_lint(
        monkeypatch,
        spec_modules={"gateway"},
        directory={"gateway"},
        hand_added=set(),
        table_text=_healthcheck_table("gateway", "task_maintenance"),
        tmp_path=tmp_path,
    )
    assert lint.check_healthcheck_roster() == 1


def test_healthcheck_roster_missing_module_fails(monkeypatch, tmp_path):
    """The audit's other drift case: a real module is absent from the table."""
    lint = _load_healthcheck_lint(
        monkeypatch,
        spec_modules={"gateway", "page_server"},
        directory={"gateway", "page_server"},
        hand_added=set(),
        table_text=_healthcheck_table("gateway"),
        tmp_path=tmp_path,
    )
    assert lint.check_healthcheck_roster() == 1


def test_healthcheck_roster_unregistered_module_fails(monkeypatch, tmp_path):
    """A module file with neither a ServiceSpec row nor a hand-added import."""
    lint = _load_healthcheck_lint(
        monkeypatch,
        spec_modules={"gateway"},
        directory={"gateway", "stray"},
        hand_added=set(),
        table_text=_healthcheck_table("gateway", "stray"),
        tmp_path=tmp_path,
    )
    assert lint.check_healthcheck_roster() == 1


def test_healthcheck_roster_sentinel_missing_fails(monkeypatch, tmp_path):
    lint = _load_healthcheck_lint(
        monkeypatch,
        spec_modules={"gateway"},
        directory={"gateway"},
        hand_added=set(),
        table_text="| `gateway.py` | service | probe |\n",
        tmp_path=tmp_path,
    )
    assert lint.check_healthcheck_roster() == 1


def test_parse_healthcheck_roster_strips_py_suffix():
    lint = importlib.import_module("scripts.lint_doc_roster")
    parsed = lint.parse_healthcheck_roster(_healthcheck_table("gateway", "lgtm"))
    assert parsed == {"gateway", "lgtm"}

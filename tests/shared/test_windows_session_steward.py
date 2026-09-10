"""The steward's pure decision surface, asserted on any platform.

The socket loop itself is Windows-only and is exercised end to end on the fleet
Windows box (see the PR's verification notes); here the exit truth table and
the helper invocation shape are pinned, because both are exactly what a
restart, a reap, or a recycled pid must not get wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.windows_session_steward import should_exit


@pytest.mark.parametrize(
    ("record_names_me", "target_alive", "record_exists", "expected"),
    [
        # Serve only while the record names this identity and the target lives.
        (True, True, True, False),
        (True, False, True, True),  # graceful stop landed — leave
        (True, False, False, True),
        (True, True, False, False),  # spawn race: record not written yet
        # A record naming a different identity always ends this steward.
        (False, True, True, True),  # same-name restart
        (False, False, True, True),  # reap wrote a new record
        (False, True, False, False),  # record gone while target alive: spawn race
        (False, False, False, True),
    ],
)
def test_should_exit_truth_table(
    record_names_me: bool, target_alive: bool, record_exists: bool, expected: bool
) -> None:
    assert (
        should_exit(
            record_names_me=record_names_me,
            target_alive=target_alive,
            record_exists=record_exists,
        )
        is expected
    )


def test_deliver_break_runs_the_verified_helper_with_the_exact_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from shared import windows_session_steward as steward

    calls: list[list[str]] = []

    def run(argv: list[str], **_: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(steward.subprocess, "run", run)
    monkeypatch.setattr(steward, "sys", SimpleNamespace(executable="/venv/python.exe"))
    accepted, detail = steward._deliver_break(Path("/home/run/sessions/s.json"), 42, 7.5)
    assert accepted and detail == "accepted"
    assert calls[0][:4] == [
        "/venv/python.exe",
        "-I",
        str(steward._HELPER),
        "/home/run/sessions/s.json",
    ]
    assert calls[0][4:6] == ["42", "7.5"]
    # A bounded deadline, passed as epoch-after-start to the helper.
    assert float(calls[0][6]) > 0


def test_deliver_break_reports_the_helpers_refusal_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from shared import windows_session_steward as steward

    def run(_argv: list[str], **_: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stderr="private console delivery refused: nope\n")

    monkeypatch.setattr(steward.subprocess, "run", run)
    accepted, detail = steward._deliver_break(Path("/s.json"), 42, 7.5)
    assert not accepted
    assert "nope" in detail

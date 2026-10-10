"""agent/graph/exec/output.py's wrap_code_output pure function test.

`wrap_code_output` is the envelope fed to LLM — agent relies on "Code execution output" prefix to identify subprocess feedback. Anchor format; changing one side will red the other side test.

`output` single param = subprocess stdout + stderr merged stream (`stderr=STDOUT`, equivalent shell 2>&1), preserves real timing order; no longer separate stdout / stderr.
return code not in envelope — framework internal signal (42 / 43) goes via ToolMessage metadata.

Timestamp is supplied by the composition caller; tests pass fixed rendered text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent.graph.exec.output import format_elapsed, wrap_code_output
from agent.graph.exec.tests.output_inputs import CropConfig
from base.clock import Clock
from base.config import settings
from tests.fixtures.pin_agent import pin_agent

_TIMESTAMP = "[2026-05-06 14:32:05]"
_TS = _TIMESTAMP  # shorthand


def test_wrap_code_output_structured(crop_config: CropConfig, output_clock: Clock):
    """code_output envelope contains output, **does not** contain exit code."""
    out = wrap_code_output(
        "hello\nwarn\n",
        agent_id=7,
        crop_config=crop_config,
        clock=output_clock,
        timeout_seconds=60,
        max_chars=30_000,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "Code execution output" in out
    assert _TS in out
    assert "hello" in out and "warn" in out
    assert "[exit" not in out


def test_wrap_code_output_cancelled_marker(crop_config: CropConfig, output_clock: Clock):
    """When cancelled, carries [cancelled by user] marker."""
    out = wrap_code_output(
        "part\n",
        agent_id=7,
        cancelled=True,
        crop_config=crop_config,
        clock=output_clock,
        timeout_seconds=60,
        max_chars=30_000,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "[cancelled by user]" in out
    assert _TS in out
    assert "part" in out


def test_wrap_code_output_appends_trailing_newline_when_missing(
    crop_config: CropConfig, output_clock: Clock
):
    """When output lacks trailing \\n, envelope still normalizes to single line — nothing to join afterwards, mainly for more stable frontend rendering."""
    out = wrap_code_output(
        "partial output",
        agent_id=7,
        crop_config=crop_config,
        clock=output_clock,
        timeout_seconds=60,
        max_chars=30_000,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "partial output" in out
    assert _TS in out


def test_wrap_code_output_no_stderr_marker_in_envelope(
    crop_config: CropConfig, output_clock: Clock
):
    """stdout/stderr already merged, envelope should not have '--- stderr ---' separator marker — keeping it directly contradicts 'merged stream' design."""
    out = wrap_code_output(
        "hello\nTraceback (most recent call last):\n  ...\n",
        agent_id=7,
        crop_config=crop_config,
        clock=output_clock,
        timeout_seconds=60,
        max_chars=30_000,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "--- stderr ---" not in out


def test_wrap_code_output_no_output_marker_when_empty(crop_config: CropConfig, output_clock: Clock):
    """When output is empty, show '(no output)' — explicitly tell agent code ran but produced no output (avoid empty envelope making LLM mistakenly think exec failed/didn't run)."""
    out = wrap_code_output(
        "",
        agent_id=7,
        crop_config=crop_config,
        clock=output_clock,
        timeout_seconds=60,
        max_chars=30_000,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "Code execution output" in out
    assert "(no output)" in out
    assert _TS in out


def test_wrap_code_output_no_output_marker_omitted_when_output_present(
    crop_config: CropConfig, output_clock: Clock
):
    """When output present, don't add '(no output)' marker — marker is only fallback for empty envelope."""
    out = wrap_code_output(
        "real output\n",
        agent_id=7,
        crop_config=crop_config,
        clock=output_clock,
        timeout_seconds=60,
        max_chars=30_000,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "(no output)" not in out


def test_wrap_code_output_no_output_marker_with_cancelled(
    crop_config: CropConfig, output_clock: Clock
):
    """Under cancel path, even if output empty, add '(no output)' marker — agent sees [cancelled by user] + (no output) knows it was interrupted and produced nothing."""
    out = wrap_code_output(
        "",
        agent_id=7,
        cancelled=True,
        crop_config=crop_config,
        clock=output_clock,
        timeout_seconds=60,
        max_chars=30_000,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "[cancelled by user]" in out
    assert "(no output)" in out
    assert _TS in out


def test_wrap_code_output_header_body_double_newline_split(
    crop_config: CropConfig, output_clock: Clock
):
    """Between header and body use \\n\\n separator (same contract as wrap_inbound, frontend splitEnvelope splits header/body by this)."""
    out = wrap_code_output(
        "body line\n",
        agent_id=7,
        crop_config=crop_config,
        clock=output_clock,
        timeout_seconds=60,
        max_chars=30_000,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    # header must be followed by double \n
    assert f"Code execution output after running for 1.0s {_TS}:\n\nbody line" in out


def test_wrap_code_output_truncates_keeps_both_ends_and_writes_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crop_config: CropConfig,
    output_clock: Clock,
):
    """Exceeds exec_output_max_chars → keep head + tail, cut middle, full output written to tmp file and path reported.
    Head carries help() overview / overview, tail carries error / result, both ends must survive."""
    from agent.graph.exec import output

    pin_agent(7)

    def overflow_dir(_agent_id: int) -> Path:
        return tmp_path / "overflow"

    monkeypatch.setattr(output, "_overflow_dir", overflow_dir)

    limit = 1000
    head_marker = "HEAD_START"
    tail_marker = "TAIL_END"
    big = head_marker + ("X" * 5000) + ("M" * (limit * 3)) + ("Y" * 5000) + tail_marker
    out = wrap_code_output(
        big,
        agent_id=7,
        max_chars=limit,
        crop_config=crop_config,
        clock=output_clock,
        timeout_seconds=60,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text

    assert head_marker in out, "head must be preserved (help overview at start)"
    assert tail_marker in out, "tail must be preserved (error / result usually at end)"
    assert "M" * (limit * 3) not in out, "middle should be cut"
    assert "output truncated" in out and "omitted" in out, (
        "marker indicates truncation + how much omitted"
    )

    # path is reported, and the file holds the complete output
    files = list((tmp_path / "overflow").glob("exec_*.txt"))
    assert len(files) == 1
    assert str(files[0]) in out, "complete output path must be reported to agent"
    assert files[0].read_text(encoding="utf-8") == big


def test_wrap_code_output_overflow_files_pruned_to_keep_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crop_config: CropConfig,
    output_clock: Clock,
):
    """Same agent repeatedly overflows keeps only recent N, prevents workspace infinite pile-up."""
    from agent.graph.exec import output

    pin_agent(7)

    def overflow_dir(_agent_id: int) -> Path:
        return tmp_path / "overflow"

    monkeypatch.setattr(output, "_overflow_dir", overflow_dir)
    monkeypatch.setattr(output, "_OVERFLOW_KEEP", 3)

    for _ in range(5):
        wrap_code_output(
            "Z" * 2000,
            agent_id=7,
            max_chars=100,
            crop_config=crop_config,
            clock=output_clock,
            timeout_seconds=60,
            timestamp=_TS,
            elapsed_seconds=1.0,
        )
    files = list((tmp_path / "overflow").glob("exec_*.txt"))
    assert len(files) == 3, "only keep recent 3"


def test_wrap_code_output_no_truncation_when_under_limit(
    crop_config: CropConfig, output_clock: Clock
):
    """≤ exec_output_max_chars returns directly unchanged, no marker added."""
    just_under = "y" * (30_000 - 100)
    out = wrap_code_output(
        just_under,
        agent_id=7,
        crop_config=crop_config,
        clock=output_clock,
        timeout_seconds=60,
        max_chars=30_000,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "output truncated" not in out
    assert just_under in out


def test_wrap_code_output_timed_out_marker(crop_config: CropConfig, output_clock: Clock):
    """When timed out, carries [timeout after Ns] marker (N from settings.sandbox.exec_timeout_seconds), no cancel marker."""
    out = wrap_code_output(
        "part\n",
        agent_id=7,
        timed_out=True,
        timeout_seconds=60,
        crop_config=crop_config,
        clock=output_clock,
        max_chars=30_000,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "[timeout after 60s]" in out
    assert "[cancelled by user]" not in out
    assert "part" in out


def test_wrap_code_output_timed_out_carries_strategy_hint(
    crop_config: CropConfig, output_clock: Clock
):
    """timeout envelope is agent's only clue to change strategy — hint must name long-task primitive; non-timeout path no hint."""
    out = wrap_code_output(
        "part\n",
        agent_id=7,
        timed_out=True,
        timeout_seconds=60,
        crop_config=crop_config,
        clock=output_clock,
        max_chars=30_000,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "run_background" in out
    assert "ava.watcher.launch" in out
    assert (
        "run_background"
        not in wrap_code_output(
            "part\n",
            agent_id=7,
            crop_config=crop_config,
            clock=output_clock,
            timeout_seconds=60,
            max_chars=30_000,
            timestamp=_TS,
            elapsed_seconds=1.0,
        ).text
    )
    assert (
        "run_background"
        not in wrap_code_output(
            "part\n",
            agent_id=7,
            cancelled=True,
            crop_config=crop_config,
            clock=output_clock,
            timeout_seconds=60,
            max_chars=30_000,
            timestamp=_TS,
            elapsed_seconds=1.0,
        ).text
    )


def test_wrap_code_output_timed_out_empty(crop_config: CropConfig, output_clock: Clock):
    """Under timeout path, even output empty, adds '(no output)' marker, hint still present."""
    out = wrap_code_output(
        "",
        agent_id=7,
        timed_out=True,
        timeout_seconds=60,
        crop_config=crop_config,
        clock=output_clock,
        max_chars=30_000,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "[timeout after 60s]" in out
    assert "(no output)" in out
    assert "run_background" in out
    assert "[cancelled by user]" not in out


def test_wrap_code_output_cancelled_wins_over_timed_out_marker(
    crop_config: CropConfig, output_clock: Clock
):
    """When both cancel and timed_out True, cancel marker shows (priority).
    However wrap_code_output only receives one bool — caller (exec_node) guarantees mutual exclusion.
    Here verify when cancelled=True, no timeout marker appears."""
    out = wrap_code_output(
        "data\n",
        agent_id=7,
        cancelled=True,
        timeout_seconds=60,
        crop_config=crop_config,
        clock=output_clock,
        max_chars=30_000,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    assert "[cancelled by user]" in out
    assert "[timeout after 60s]" not in out


def test_wrap_code_output_no_timestamp_when_disabled(
    monkeypatch: pytest.MonkeyPatch, crop_config: CropConfig, output_clock: Clock
):
    """settings.general.message_timestamps=False → header drops the timestamp and the
    space before the colon, keeping any marker intact."""
    assert wrap_code_output(
        "hello\n",
        agent_id=7,
        crop_config=crop_config,
        clock=output_clock,
        timeout_seconds=60,
        max_chars=30_000,
        elapsed_seconds=1.0,
    ).text.startswith("Code execution output after running for 1.0s:\n\n")
    assert (
        _TS
        not in wrap_code_output(
            "hello\n",
            agent_id=7,
            crop_config=crop_config,
            clock=output_clock,
            timeout_seconds=60,
            max_chars=30_000,
            elapsed_seconds=1.0,
        ).text
    )
    # marker survives, still no timestamp / stray space
    out = wrap_code_output(
        "part\n",
        agent_id=7,
        cancelled=True,
        crop_config=crop_config,
        clock=output_clock,
        timeout_seconds=60,
        max_chars=30_000,
        elapsed_seconds=1.0,
    ).text
    assert "Code execution output after running for 1.0s [cancelled by user]:\n\n" in out
    assert _TS not in out


# ── P0 #2100: a crashed exec with empty output must never read as "(no output)" ──


def _boot_crash(exc: Exception, code_reached: bool | None) -> object:
    from agent.graph.exec._result import _ExecCrashed

    return _ExecCrashed(output="", exc=exc, full_traceback=None, code_reached=code_reached)


def _dispatch(result: object, monkeypatch: pytest.MonkeyPatch) -> tuple[bool, str]:
    from agent.graph.exec.node import _dispatch_exec_result

    del monkeypatch
    halted, envelope, _ = _dispatch_exec_result(
        result,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        7,
        referenced_messages=(),
        elapsed_seconds=1.0,
        read_sandbox=lambda field: getattr(settings.sandbox, field),
        clock=Clock.from_settings(),
    )
    return halted, envelope.text


def test_dispatch_boot_crash_reports_not_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The outage shape: child crashed on bootstrap fetch, stdout empty. The
    agent must read "code was NOT executed", never "(no output)"."""
    from agent.graph.exec._result import ExecChildError

    exc = ExecChildError("BootstrapFetchError", "could not fetch cluster config", "tb")
    halted, text = _dispatch(_boot_crash(exc, False), monkeypatch)
    assert halted is False
    assert "Code execution output" in text
    assert "the code was NOT executed" in text
    assert "BootstrapFetchError: could not fetch cluster config" in text
    assert "(no output)" not in text


def test_dispatch_crash_with_unknown_code_reached_stays_honest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Older child / missing envelope: say execution status is unknown."""
    from agent.graph.exec._result import ExecChildError

    exc = ExecChildError("exec_subprocess_aborted", "child exited without a result envelope", None)
    halted, text = _dispatch(_boot_crash(exc, None), monkeypatch)
    assert halted is False
    assert "whether the code executed is unknown" in text
    assert "(no output)" not in text


def test_dispatch_crash_after_code_ran_but_printed_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Code phase reached: missing output does not prove completion or no effects."""
    from agent.graph.exec._result import ExecChildError

    exc = ExecChildError("OSError", "result envelope write failed", None)
    halted, text = _dispatch(_boot_crash(exc, True), monkeypatch)
    assert halted is False
    assert "your code may have had effects, but no output was recovered" in text
    assert "inspect state before retrying" in text
    assert "(no output)" not in text


def test_dispatch_crash_with_output_keeps_the_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash WITH stdout keeps the agent-facing traceback — no marker added."""
    from agent.graph.exec._result import ExecChildError, _ExecCrashed

    result = _ExecCrashed(
        output="Traceback (most recent call last):\n  boom\n",
        exc=ExecChildError("ValueError", "boom", None),
        code_reached=True,
    )
    halted, text = _dispatch(result, monkeypatch)
    assert halted is False
    assert "Traceback (most recent call last)" in text
    assert "(no output)" not in text


def test_dispatch_boot_crash_emits_the_boot_failed_event(
    loguru_records: list[dict[str, Any]],
) -> None:
    """P2 #2102: a boot-phase failure (code_reached=False) emits the WARNING
    exec_child_boot_failed event the alert rule reads; the unknown case does not."""
    from agent.graph.exec._result import ExecChildError
    from agent.graph.exec.node import _dispatch_exec_result

    boot_exc = ExecChildError("BootstrapFetchError", "down", None)
    _dispatch_exec_result(
        _boot_crash(boot_exc, False),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        42,
        referenced_messages=(),
        elapsed_seconds=1.0,
        read_sandbox=lambda field: getattr(settings.sandbox, field),
        clock=Clock.from_settings(),
    )
    boot = [r for r in loguru_records if r["extra"].get("event") == "exec_child_boot_failed"]
    assert len(boot) == 1
    assert boot[0]["level"].name == "WARNING"
    assert boot[0]["extra"]["agent_id"] == 42
    assert boot[0]["extra"]["exc_type"] == "BootstrapFetchError"
    assert boot[0]["extra"]["exc_msg"] == "down"

    loguru_records.clear()
    unknown_exc = ExecChildError("exec_subprocess_aborted", "gone", None)
    _dispatch_exec_result(
        _boot_crash(unknown_exc, None),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        42,
        referenced_messages=(),
        elapsed_seconds=1.0,
        read_sandbox=lambda field: getattr(settings.sandbox, field),
        clock=Clock.from_settings(),
    )
    assert not [r for r in loguru_records if r["extra"].get("event") == "exec_child_boot_failed"]


def test_crashed_no_output_body_forms() -> None:
    from agent.graph.exec._result import ExecChildError
    from agent.graph.exec.output import crashed_no_output_body

    body = crashed_no_output_body(ExecChildError("X", "y", None), code_reached=False)
    assert "NOT executed" in body
    assert "X: y" in body
    body2 = crashed_no_output_body(ValueError("plain"), code_reached=None)
    assert "ValueError: plain" in body2


def test_overflow_archive_uses_explicit_host_identity(
    unit_home: Path, crop_config: CropConfig, output_clock: Clock
) -> None:
    """The native output path belongs to the host argument, not the child SDK slot."""
    pin_agent(999)
    body = "archive " * 100
    wrapped = wrap_code_output(
        body,
        max_chars=100,
        agent_id=11,
        crop_config=crop_config,
        clock=output_clock,
        timeout_seconds=60,
        timestamp=_TS,
        elapsed_seconds=1.0,
    ).text
    directory = unit_home / "workspaces" / "11" / ".exec_output"
    files = list(directory.glob("exec_*.txt"))
    assert len(files) == 1
    assert files[0].read_text() == body
    assert str(files[0]) in wrapped
    assert not (unit_home / "workspaces" / "999").exists()


def test_archive_clock_and_rendered_timestamp_are_explicit(
    unit_home: Path, crop_config: CropConfig
) -> None:
    """Archive timezone and envelope text come from caller inputs, not host settings."""
    from datetime import UTC, datetime

    from base.clock import ClockConfig

    now = datetime(2026, 10, 9, 1, 2, 3, 456789, tzinfo=UTC)
    clock = Clock(ClockConfig("Asia/Shanghai", "Asia/Shanghai", False), now=lambda: now)
    wrapped = wrap_code_output(
        "x" * 200,
        agent_id=13,
        crop_config=crop_config,
        clock=clock,
        timeout_seconds=17,
        max_chars=100,
        timestamp="[caller supplied timestamp]",
        elapsed_seconds=1.0,
    ).text
    assert wrapped.startswith(
        "Code execution output after running for 1.0s [caller supplied timestamp]:\n\n"
    )
    # The runtime owns workspace layout; inspect the path announced in the body.
    files = list(unit_home.rglob("exec_20261009_090203_456789.txt"))
    assert len(files) == 1
    assert str(files[0]) in wrapped
    assert files[0].read_text() == "x" * 200


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0.0, "0ms"),
        (0.85, "850ms"),
        (4.24, "4.2s"),
        (53.4, "53s"),
        (60.0, "1min 0s"),
        (113.0, "1min 53s"),
        (1200.0, "20min 0s"),
        (3723.0, "1h 2min 3s"),
    ],
)
def test_format_elapsed(seconds: float, text: str):
    assert format_elapsed(seconds) == text


@pytest.mark.parametrize(
    ("kwargs", "header"),
    [
        ({}, "Code execution output after running for 1min 53s [ts]:"),
        (
            {"cancelled": True},
            "Code execution output after running for 1min 53s [cancelled by user] [ts]:",
        ),
        (
            {"timed_out": True},
            "Code execution output after running for 1min 53s [timeout after 60s] [ts]:",
        ),
        ({"timestamp": None}, "Code execution output after running for 1min 53s:"),
    ],
)
def test_header_carries_elapsed_in_every_ending(
    kwargs: dict[str, object], header: str, crop_config: CropConfig, output_clock: Clock
):
    params: dict[str, object] = {"timestamp": "[ts]", **kwargs}
    out = wrap_code_output(
        "x\n",
        agent_id=7,
        crop_config=crop_config,
        clock=output_clock,
        timeout_seconds=60,
        max_chars=30_000,
        elapsed_seconds=113.0,
        **params,  # type: ignore[arg-type]
    ).text
    assert out.startswith(header + "\n\n")


@pytest.mark.parametrize(
    ("kind", "status", "halted"),
    [
        ("done", "completed", False),
        ("timed_out", "timed_out", False),
        ("cancelled", "cancelled", True),
        ("crashed", "failed", False),
    ],
)
def test_dispatch_reports_status_and_body_start(kind: str, status: str, halted: bool) -> None:
    from agent.graph.exec._result import _ExecCancelled, _ExecCrashed, _ExecDone, _ExecTimedOut
    from agent.graph.exec.node import _dispatch_exec_result

    class _Events:
        def emit(self, _event: str) -> None: ...

    class _Ctx:
        event_publisher = _Events()

    results: dict[str, object] = {
        "done": _ExecDone(output="o\n"),
        "timed_out": _ExecTimedOut(output="o\n"),
        "cancelled": _ExecCancelled(output="o\n"),
        "crashed": _ExecCrashed(output="o\n", exc=ValueError("x"), full_traceback=None),
    }
    got_halted, envelope, got_status = _dispatch_exec_result(
        results[kind],  # type: ignore[arg-type]
        _Ctx(),  # type: ignore[arg-type]
        7,
        elapsed_seconds=113.0,
        read_sandbox=lambda field: getattr(settings.sandbox, field),
        clock=Clock.from_settings(),
    )
    assert (got_halted, got_status.value) == (halted, status)
    assert envelope.text[envelope.body_start :].startswith("o\n")
    assert envelope.text.startswith("Code execution output after running for 1min 53s")

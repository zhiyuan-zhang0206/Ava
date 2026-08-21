"""Accumulation-time byte cap on exec output, and its contract with the
downstream `_exec_output.py` envelope.

Two caps guard one stream. `StreamingTextIO` bounds the buffer WHILE the
agent's code runs (a runaway `print` loop must not grow the agent process until
it is OOM-killed); `wrap_code_output` / `truncate_both_ends` bound what reaches
the LLM AFTER the exec finished. This module pins that they agree: the first cap
keeps both ends so the second still has both ends to render, the true produced
length survives into the banner and the instrumentation log line, and the
overflow archive stops claiming to hold the full output once the middle is gone.

The other half of the contract — that the run is truncated, never killed — is
pinned end-to-end through `_run_in_subprocess` (the budget lives in the
parent's accumulator; the child ships raw chunks).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent.graph._exec_output import wrap_code_output
from agent.graph._exec_stream import StreamCap, StreamingTextIO

# ---------------------------------------------------------------------------
# The accumulator: head + rolling tail under a fixed budget
# ---------------------------------------------------------------------------


def test_uncapped_output_is_returned_byte_for_byte() -> None:
    """Under the budget the accumulator is transparent — no marker, no cap
    record. Every existing caller sees exactly what the code printed."""
    stream = StreamingTextIO(max_chars=1000)
    stream.write("hello\n")
    stream.write("world\n")

    assert stream.getvalue() == "hello\nworld\n"
    assert stream.cap() is None


def test_cap_keeps_head_and_tail_and_drops_the_middle() -> None:
    """Past the budget the first half is pinned and the last half rolls, with an
    explicit marker where the middle went — the same head+tail discipline the
    downstream envelope uses, so the two layers stay renderable together."""
    budget = 1000
    stream = StreamingTextIO(max_chars=budget)
    stream.write("HEAD_START")
    stream.write("M" * 50_000)
    stream.write("TAIL_END")

    value = stream.getvalue()
    assert value.startswith("HEAD_START")
    assert value.endswith("TAIL_END")
    assert "M" * 1000 not in value, "the middle must be dropped, not merely marked"
    assert "dropped here DURING execution" in value
    # Retained text is the budget plus the (short, fixed) marker — bounded no
    # matter how much the code printed.
    assert len(value) < budget + 300, f"retained {len(value)} chars for a {budget} budget"


def test_cap_keeps_counting_the_true_produced_length() -> None:
    """The dropped middle is gone from memory but not from the accounting: the
    `StreamCap` carries what the code actually produced, which is what the
    envelope's banner and the instrumentation log line report."""
    stream = StreamingTextIO(max_chars=100)
    for _ in range(500):
        stream.write("0123456789")

    cap = stream.cap()
    assert cap == StreamCap(produced_chars=5000, budget_chars=100)
    assert f"{5000:,} chars produced in total" in stream.getvalue()


def test_rolling_tail_keeps_the_most_recent_writes() -> None:
    """The tail is a rolling window, so the last thing the code printed — the
    result, or the traceback that explains the run — always survives even
    though everything before it was dropped."""
    stream = StreamingTextIO(max_chars=100)
    for i in range(1000):
        stream.write(f"line {i}\n")

    value = stream.getvalue()
    assert "line 999\n" in value
    assert "line 500\n" not in value


def test_a_single_oversized_write_is_sliced_not_dropped_whole() -> None:
    """One `print(huge_blob)` is the motivating accident. Its tail end must
    still land in the window rather than the whole write being discarded for
    not fitting."""
    stream = StreamingTextIO(max_chars=100)
    stream.write("h" * 10)
    stream.write("B" * 100_000 + "LAST")

    value = stream.getvalue()
    assert value.startswith("h" * 10)
    assert value.endswith("LAST")
    assert stream.cap() is not None


# ---------------------------------------------------------------------------
# Live streaming inherits the bound
# ---------------------------------------------------------------------------


def test_live_stream_is_bounded_and_says_so_exactly_once() -> None:
    """Redis pushes come off the accumulator, so they inherit the bound. Past
    the budget the retained text is a rolling window with no append-only
    increment to publish — one notice goes out, then nothing; the frontend
    picks up the head+tail envelope when ExecOutput upserts on completion."""
    stream = StreamingTextIO(max_chars=100)
    stream.write("a" * 40)
    first = stream.take_pending()
    assert first == "a" * 40
    assert stream.take_pending() == "", "nothing new to publish"

    published = [first]
    for _ in range(100):
        stream.write("z" * 1000)
        published.append(stream.take_pending())

    notices = [p for p in published if "output budget reached" in p]
    assert len(notices) == 1, f"expected exactly one cap notice, got {len(notices)}"
    assert published[-1] == "", "publishing stops once the window starts rolling"
    assert sum(len(p) for p in published) < 500, "the wire must not carry 100k chars"


# ---------------------------------------------------------------------------
# Compatibility with the downstream truncate / overflow envelope
# ---------------------------------------------------------------------------


@pytest.fixture
def _overflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the workspace overflow ring into tmp_path."""
    import ava
    from agent.graph import _exec_output

    monkeypatch.setattr(ava._boot, "_agent_id", 7)
    monkeypatch.setattr(_exec_output, "_overflow_dir", lambda: tmp_path / "overflow")
    return tmp_path / "overflow"


def test_envelope_still_has_both_ends_after_the_accumulation_cap(_overflow: Path) -> None:
    """The compatibility contract: the accumulator keeps budget/2 at each end
    and `truncate_both_ends` slices max_chars/2 off each end, so with the
    budget >= the inline cap the envelope's head comes entirely out of the
    kept head and its tail entirely out of the kept tail."""
    stream = StreamingTextIO(max_chars=2000)
    stream.write("HEAD_START")
    stream.write("M" * 100_000)
    stream.write("TAIL_END")

    out = wrap_code_output(stream.getvalue(), max_chars=1000, stream_cap=stream.cap())

    assert "HEAD_START" in out, "head must survive both caps"
    assert "TAIL_END" in out, "tail must survive both caps"
    assert "output truncated" in out and "omitted" in out


def test_envelope_banner_reports_the_true_produced_length(_overflow: Path) -> None:
    """Without this the agent reads the capped length as the real one and has no
    idea how much output it actually generated."""
    stream = StreamingTextIO(max_chars=2000)
    stream.write("X" * 250_000)

    out = wrap_code_output(stream.getvalue(), max_chars=1000, stream_cap=stream.cap())

    assert f"{250_000:,} chars produced" in out
    assert "the dropped middle is unrecoverable" in out
    assert "full output at" not in out, "the archive no longer holds the full output"


def test_envelope_still_promises_the_full_output_when_uncapped(_overflow: Path) -> None:
    """The uncapped path is unchanged: the archive really is complete, so the
    banner keeps saying so (and the ava_code plugin's reuse of
    `truncate_both_ends` keeps its wording)."""
    big = "HEAD_START" + ("M" * 5000) + "TAIL_END"
    out = wrap_code_output(big, max_chars=1000)

    assert "full output at" in out
    assert "produced" not in out
    (archived,) = list(_overflow.glob("exec_*.txt"))
    assert archived.read_text(encoding="utf-8") == big


def test_overflow_archive_says_it_is_not_the_full_output(_overflow: Path) -> None:
    """An agent that greps the archive and finds nothing must be able to tell
    "never printed" from "dropped mid-run" — otherwise the miss reads as proof."""
    stream = StreamingTextIO(max_chars=2000)
    stream.write("X" * 250_000)

    wrap_code_output(stream.getvalue(), max_chars=1000, stream_cap=stream.cap())

    (archived,) = list(_overflow.glob("exec_*.txt"))
    text = archived.read_text(encoding="utf-8")
    assert text.startswith("[archive note:")
    assert "NOT the full output" in text
    assert f"{250_000:,} chars were produced" in text
    assert len(text) < 3000, "the archive inherits the accumulation bound"


def test_instrumentation_logs_the_true_length_not_the_capped_one(
    _overflow: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`[exec output chars]` is how max_chars gets tuned from a real
    distribution. Fed the capped length it would report the budget forever and
    the runaway execs would be invisible in the data."""
    from agent.graph import _exec_output

    logged: list[int] = []

    def _capture(_msg: str, **kw: object) -> None:
        if "n" in kw:
            logged.append(int(kw["n"]))  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(_exec_output.logger, "info", _capture)

    stream = StreamingTextIO(max_chars=2000)
    stream.write("X" * 250_000)
    wrap_code_output(stream.getvalue(), max_chars=1000, stream_cap=stream.cap())

    assert logged == [250_000]


# ---------------------------------------------------------------------------
# End to end: truncate and continue, never kill
# ---------------------------------------------------------------------------


async def test_runaway_print_loop_is_truncated_and_the_run_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of truncate-over-kill: a runaway loop comes back as a
    normal `_ExecDone` with bounded output and an explicit marker, so the model
    stays in the loop and can self-correct. The exec result taxonomy is
    unchanged — no new failure kind."""
    from agent.graph._exec import _ExecDone
    from agent.graph._exec_subprocess import _run_in_subprocess

    budget = 5000
    # The accumulation budget lives in the PARENT's StreamingTextIO — the
    # child ships raw chunks and the parent accumulates/truncates. So the
    # in-process monkeypatch still reaches it.
    monkeypatch.setattr("shared.config.settings.sandbox.exec_output_accumulation_max_chars", budget)

    result, _payload = await _run_in_subprocess(
        code="for i in range(20000): print('spam', i)\nprint('DONE_MARKER')",
        agent_id=1,
        cancel_event=asyncio.Event(),
        timeout=60.0,
        chunk_publisher=None,
    )

    assert isinstance(result, _ExecDone), f"the run must not be killed, got {type(result).__name__}"
    assert result.stream_cap is not None, "the cap must have engaged"
    assert result.stream_cap.produced_chars > 100_000
    assert len(result.output) < budget + 300, f"output not bounded: {len(result.output)} chars"
    assert result.output.startswith("spam 0\n"), "the head is pinned"
    assert "DONE_MARKER" in result.output, "the loop ran to completion, the tail proves it"
    assert "dropped here DURING execution" in result.output


# ---------------------------------------------------------------------------
# The budget is a validated settings field
# ---------------------------------------------------------------------------


def test_budget_below_the_inline_cap_is_refused_at_startup() -> None:
    """A budget under `exec_output_max_chars` would hand the envelope less than
    it slices, so its "head" would reach into the accumulator's dropped middle.
    Fail the operator's config loudly instead of rendering an incoherent
    envelope."""
    from pydantic import ValidationError

    from shared.config.sandbox import SandboxSettings

    with pytest.raises(ValidationError, match="must be >= exec_output_max_chars"):
        SandboxSettings.model_validate(
            {"exec_output_max_chars": 30_000, "exec_output_accumulation_max_chars": 1000}
        )

    ok = SandboxSettings.model_validate(
        {"exec_output_max_chars": 1000, "exec_output_accumulation_max_chars": 1000}
    )
    assert ok.exec_output_accumulation_max_chars == 1000

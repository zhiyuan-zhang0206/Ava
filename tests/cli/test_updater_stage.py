"""`cli.commands._updater_stage` — the updater log's per-stage wall-clock markers.

cmd.exe expands `%TIME%` once per command line (parse time), so the Windows
updater ladder cannot timestamp its own `echo` lines; each stage marker is a
tiny `python -m cli.commands._updater_stage <stage>` invocation instead. These
tests pin the marker's shape — the one thing the ladder's call sites and any
later parser both depend on.
"""

from __future__ import annotations

import re

import pytest

from cli.commands import _updater_stage


def test_now_marker_matches_the_loguru_hhmmss_mmm_shape() -> None:
    """The marker must line up with the loguru `HH:MM:SS.mmm` prefix already in
    the updater log (loguru lines and stage markers are read together)."""
    marker = _updater_stage.now_marker()
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2}\.\d{3}", marker), marker


def test_stage_line_carries_the_updater_prefix_and_wall_clock() -> None:
    line = _updater_stage.stage_line("fetch")
    assert line.startswith("[updater] stage fetch @ ")
    assert re.search(r"@ \d{2}:\d{2}:\d{2}\.\d{3}$", line)


def test_main_prints_the_joined_stage_name(capsys: pytest.CaptureFixture[str]) -> None:
    assert _updater_stage.main(["fetch"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("[updater] stage fetch @ ")


def test_main_joins_multiword_stage_names(capsys: pytest.CaptureFixture[str]) -> None:
    _updater_stage.main(["uv", "sync"])
    assert capsys.readouterr().out.startswith("[updater] stage uv sync @ ")

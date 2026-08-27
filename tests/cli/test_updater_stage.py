"""The cmd.exe ladder's per-stage marker emitter (Task #1820).

`ops.cluster_deploy` inserts `python -m cli.commands._updater_stage <name>`
between the Windows updater ladder's steps; this module's printed line is the
one `ops.updater_outcome._STAGE_LINE_RE` pairs into per-stage durations. The
emitter and the parser are one contract, so they are tested as one: a marker
printed in a shape the reader does not recognise is a marker nothing reads.
"""

from __future__ import annotations

import re

import pytest

from cli.commands import _updater_stage


def test_main_prints_the_marker_line_the_reader_pairs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = _updater_stage.main(["fetch"])

    assert rc == 0
    line = capsys.readouterr().out.strip()
    # Exactly the shape `ops.updater_outcome._STAGE_LINE_RE` recognises.
    match = re.fullmatch(r"\[updater\] stage=fetch t=(\d+(?:\.\d+)?)", line)
    assert match is not None
    assert float(match.group(1)) >= 0


def test_main_without_a_name_prints_unknown(capsys: pytest.CaptureFixture[str]) -> None:
    _updater_stage.main([])
    assert capsys.readouterr().out.strip().startswith("[updater] stage=unknown t=")


def test_main_defaults_to_process_argv(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["_updater_stage", "uv"])
    assert _updater_stage.main() == 0
    assert capsys.readouterr().out.strip().startswith("[updater] stage=uv t=")

"""`ava pitr drill` parse-layer gates: chain/candidate XOR + target-wall format (task #4092 B4)."""

from __future__ import annotations

import pytest

from cli.main import _build_parser

_TARGET = ["--target-lsn", "26/A03520B0", "--target-wall", "2026-09-13 13:13:03+08"]


def test_requires_exactly_one_candidate_source(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        _build_parser().parse_args(["pitr", "drill", *_TARGET, "--scratch", "./s"])
    assert raised.value.code == 2
    assert "one of the arguments --chain --candidate is required" in capsys.readouterr().err

    with pytest.raises(SystemExit) as raised:
        _build_parser().parse_args(
            [
                "pitr",
                "drill",
                "--chain",
                "c",
                "--candidate",
                "./m.json",
                *_TARGET,
                "--scratch",
                "./s",
            ]
        )
    assert raised.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_target_wall_is_validated_at_parse_time(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        _build_parser().parse_args(
            [
                "pitr",
                "drill",
                "--chain",
                "c",
                "--target-lsn",
                "26/A",
                "--target-wall",
                "soon",
                "--scratch",
                "./s",
            ]
        )
    assert raised.value.code == 2
    assert "argument --target-wall:" in capsys.readouterr().err

    with pytest.raises(SystemExit) as raised:
        _build_parser().parse_args(
            [
                "pitr",
                "drill",
                "--chain",
                "c",
                "--target-lsn",
                "26/A",
                "--target-wall",
                "2026-09-13 13:13:03",
                "--scratch",
                "./s",
            ]
        )
    assert raised.value.code == 2
    assert "UTC offset" in capsys.readouterr().err


def test_valid_invocation_parses() -> None:
    ns = _build_parser().parse_args(["pitr", "drill", "--chain", "c", *_TARGET, "--scratch", "./s"])
    assert ns.chain == "c" and ns.candidate is None
    assert ns.target_wall == "2026-09-13 13:13:03+08"

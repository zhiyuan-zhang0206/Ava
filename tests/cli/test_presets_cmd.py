"""`ava presets` CLI: parse-layer gates + `--config` boundary validation (task #4092 B4).

Same thin-client shape as `test_schedules_cmd.py`; the handler-level gate runs
before any command code, and `--config` is validated by an argparse type.
"""

from __future__ import annotations

import pytest

from cli.main import _build_parser


def test_update_requires_at_least_one_field_at_parse_time(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _build_parser().parse_args(["presets", "update", "nightly"])
    assert args.func(args) == 2
    assert "at least one" in capsys.readouterr().err


def test_update_passes_the_parse_gate_with_one_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.commands import presets as _presets

    def fake(_args: object) -> int:
        return 0

    monkeypatch.setattr(_presets, "h_presets_update", fake)
    args = _build_parser().parse_args(["presets", "update", "nightly", "--label", "x"])
    assert args.func(args) == 0


def test_config_is_validated_as_a_json_object(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        _build_parser().parse_args(
            ["presets", "create", "--name", "n", "--label", "l", "--config", "{bad"]
        )
    assert raised.value.code == 2
    assert "argument --config:" in capsys.readouterr().err

    with pytest.raises(SystemExit) as raised:
        _build_parser().parse_args(["presets", "update", "n", "--config", "[]"])
    assert raised.value.code == 2
    assert "config must be a JSON object" in capsys.readouterr().err


def test_config_valid_object_parses() -> None:
    ns = _build_parser().parse_args(
        ["presets", "create", "--name", "n", "--label", "l", "--config", '{"a": 1}']
    )
    assert ns.config == '{"a": 1}'

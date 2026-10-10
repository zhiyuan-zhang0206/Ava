"""Settings-free syntax and command-option contracts for CLI consumers."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cli.parsers import build_parser, command_options, parse_args


@pytest.mark.parametrize(
    "argv",
    [["memory", "search", "text"], ["cluster", "destroy"], ["agents", "context", "7"]],
)
def test_parsing_keeps_defaults_and_bound_handlers(argv: list[str]) -> None:
    assert vars(parse_args(argv)) == vars(build_parser().parse_args(argv))


@pytest.mark.parametrize(
    ("argv", "code"),
    [([], 2), (["unknown-command"], 2), (["--help"], 0), (["logs", "retention", "--help"], 0)],
)
def test_help_and_errors_keep_argparse_output(
    argv: list[str], code: int, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as original:
        build_parser().parse_args(argv)
    expected = capsys.readouterr()

    with pytest.raises(SystemExit) as public:
        parse_args(argv)

    assert public.value.code == original.value.code == code
    assert capsys.readouterr() == expected


def test_omitted_arguments_read_the_process_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["ava", "memory", "search", "text", "--limit", "8"])
    args = parse_args()
    assert args.query == "text" and args.limit == 8


def test_options_keep_command_paths_aliases_and_fresh_snapshots() -> None:
    options = command_options()
    assert options[("ava",)] == {"--help"}
    assert "--keep-infra" in options[("ava", "stop")]
    assert "--keep-infra" not in options[("ava", "start")]
    timeline = ("ava", "agents", "timeline")
    context = ("ava", "agents", "context")
    assert options[timeline] == options[context] == {"--help", "--limit", "--before"}
    options[context].clear()
    assert "--limit" in options[timeline]
    assert "--limit" in command_options()[context]


def test_public_queries_do_not_load_settings_or_dispatch(tmp_path: Path) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env["AVA_HOME"] = str(tmp_path)
    child = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "from cli.parsers import command_options, parse_args; import sys; "
            "assert command_options(); assert callable(parse_args(['status']).func); "
            "assert 'base.config' not in sys.modules; "
            "assert 'cli.commands.lifecycle.status' not in sys.modules",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert child.returncode == 0, child.stderr

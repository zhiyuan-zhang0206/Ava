"""The verbs that bring a unit up keep their loguru records.

Importing `base.log` drops loguru's default handler, so a CLI process that
opens no sink discards every record it writes through loguru. On the start
path some warnings exist only there: a skipped pgvector pre-create, untracked
migration files that will not be applied. `ava start`, `ava restart`
and `ava lgtm on|off` (every in-process `cmd_start`)
open the sinks `base.log.init_cli_process` gives: stderr, the unit's
`logs/cli-<verb>.log` and the event pipeline, without a `service_started`
row. Other verbs print to the caller's terminal and open none.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

import cli.main as _main
from cli import parsers
from cli.parsers import build_parser

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# The real `ava init` then `ava start` dispatch into a fresh home: identity
# publication, then the sinks, then a service phase replaced by the real
# pgvector pre-create against a port nothing listens on.
_FIRST_START = r"""
import os
import sys
import types
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from cli import main, start_intent
from base import cluster
home = Path(os.environ["AVA_HOME"])
checkout = home.parent / "checkout"
checkout.mkdir()
start_intent._checkout = lambda: checkout
cluster.port_free = lambda _port: True
def start(**kwargs):
    from base.cluster import ensure_pgvector_extension
    ensure_pgvector_extension(
        "ava", base_admin_url="postgresql://probe@127.0.0.1:1/postgres?connect_timeout=5"
    )
    return 0
sys.modules["cli.commands.lifecycle.start"] = types.SimpleNamespace(cmd_start=start)
sys.modules["cli.commands.lifecycle.root_driver"] = types.SimpleNamespace(complete_boot_start=lambda: None)
assert main.main(["init", "--serve-gateway", "--serve-agent-runner", "--machine-name", "probe"]) == 0
raise SystemExit(main.main(["start"]))
"""


def test_ava_start_writes_a_loguru_warning_to_stderr_and_its_log(tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env.update(AVA_HOME=str(home))
    child = subprocess.run(  # noqa: S603 — this interpreter, fixed code, a private home
        [sys.executable, "-I", "-B", "-c", _FIRST_START, str(_REPO_ROOT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert child.returncode == 0, child.stderr

    stderr = _ANSI.sub("", child.stderr)
    [warning] = [line for line in stderr.splitlines() if "[pgvector] pre-create skipped" in line]
    assert "WARNING" in warning and "OperationalError" not in warning
    assert "127.0.0.1" in warning  # the connection error itself, not a `%s` placeholder
    assert "service started" not in stderr

    records = [
        json.loads(line)
        for line in (home / "logs/cli-start.log").read_text().splitlines()
        if line.strip()
    ]
    assert any("[pgvector] pre-create skipped" in json.dumps(r) for r in records)
    assert not any("service_started" in json.dumps(r) for r in records)


def _dispatching(sink: list[str]) -> argparse.ArgumentParser:
    def dispatched(_args: argparse.Namespace) -> int:
        sink.append("dispatched")
        return 0

    parser = argparse.ArgumentParser(prog="ava")
    parser.add_argument("words", nargs="*")
    parser.set_defaults(func=dispatched)
    return parser


@pytest.mark.parametrize(
    ("argv", "name"),
    [
        (["restart"], "cli-restart"),
        (["lgtm", "on"], "cli-lgtm"),
        (["lgtm", "off"], "cli-lgtm"),
        (["status"], None),
        (["lgtm", "status"], None),
    ],
)
def test_only_the_verbs_that_bring_a_unit_up_open_sinks_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, cli_log_sinks: list[str], argv: list[str], name: str | None
) -> None:
    monkeypatch.setattr(
        parsers, "build_parser", _ignoring_retention(lambda: _dispatching(cli_log_sinks))
    )

    assert _main.main(argv) == 0

    assert cli_log_sinks == ([name, "dispatched"] if name else ["dispatched"])


def test_a_settings_failure_while_opening_sinks_keeps_its_actionable_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Opening the sinks builds Settings; a runner whose gateway is unreachable
    fails there with the same message the command itself would print."""
    import base.log
    from base.host.env.bootstrap import BootstrapFetchError

    def unreachable(*, name: str) -> None:
        raise BootstrapFetchError(f"could not fetch cluster config ({name})")

    dispatched: list[str] = []
    monkeypatch.setattr(base.log, "init_cli_process", unreachable)
    monkeypatch.setattr(
        parsers, "build_parser", _ignoring_retention(lambda: _dispatching(dispatched))
    )

    assert _main.main(["restart"]) == 1

    assert dispatched == []
    assert "could not fetch cluster config (cli-restart)" in capsys.readouterr().err


def _ignoring_retention[T](callback: Callable[[], T]) -> Callable[..., T]:
    def invoke(*_args: object, **_kwargs: object) -> T:
        return callback()

    return invoke


def test_main_reuses_only_the_explicit_caller_child_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    owners: list[list[subprocess.Popen[bytes]]] = []

    def dispatched(
        _args: argparse.Namespace, *, retained_children: list[subprocess.Popen[bytes]]
    ) -> int:
        owners.append(retained_children)
        return 0

    def stopped(*, retained_children: list[subprocess.Popen[bytes]], **_kwargs: object) -> int:
        owners.append(retained_children)
        return 0

    monkeypatch.setattr("cli.start_intent.run_start", dispatched)
    monkeypatch.setattr("cli.commands.lifecycle.stop.cmd_stop", stopped)
    shared: list[subprocess.Popen[bytes]] = []
    assert _main.main(["start"], retained_children=shared) == 0
    assert _main.main(["stop", "--yes"], retained_children=shared) == 0
    assert owners[0] is shared and owners[1] is shared
    assert _main.main(["start"]) == 0
    assert _main.main(["start"]) == 0
    assert owners[2] is not shared and owners[3] is not owners[2]


def test_inspection_parser_cannot_launch_without_a_child_owner() -> None:
    args = build_parser().parse_args(["start"])
    with pytest.raises(ValueError, match="caller-owned child retention"):
        args.func(args)

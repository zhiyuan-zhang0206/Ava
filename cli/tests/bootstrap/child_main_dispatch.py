"""Fresh-interpreter probes for the CLI entry's import and admission boundaries.

Usage: `child_main_dispatch.py <scenario> [args]`. Every probe asserts what the
interpreter has (not) imported, so this module imports only the standard library
at top level and each scenario imports first-party code itself, in the order it
is checking.
"""

import importlib.abc
import socket
import sys
from typing import Any


def _defer_logging() -> None:
    """A settings-free entry can import the parser before choosing its path."""
    import cli.main  # noqa: F401  # pyright: ignore[reportUnusedImport]

    assert "base.log" not in sys.modules
    assert "base.config" not in sys.modules
    assert not any(
        name in sys.modules
        for name in (
            "cli.commands.agents.control",
            "cli.commands.agents.notices",
            "cli.commands.agents.timeline",
            "cli.commands.agents.impersonation",
            "cli.commands.agents.impersonation_relay",
            "cli.commands.agents.impersonation_adapters",
        )
    )


class _DenyConfig(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
        if fullname == "base.config":
            raise AssertionError("forbidden early import: " + fullname)


def _parse_real_subcommands() -> None:
    """Build the real argparse tree and parse real subcommands without dispatching."""
    from cli.parsers import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "cluster",
            "db-authority",
            "issue-unit",
            "--machine",
            "unit",
            "--home",
            "/unit",
            "--out",
            "/unused/bundle",
        ]
    )
    assert args.machine == "unit"
    args = parser.parse_args(["agents", "restart", "7", "--config", "{}"])
    assert args.agent_id == 7
    assert args.config == "{}"
    args = parser.parse_args(["notices", "list", "--agent", "7"])
    assert args.agent == 7
    args = parser.parse_args(["impersonate", "list", "--agent", "7"])
    assert args.agent_id == 7
    args = parser.parse_args(
        [
            "impersonate",
            "relay",
            "7",
            "--session",
            "0",
            "--provider",
            "codex",
            "--thread-id",
            "thread",
        ]
    )
    assert args.session_id == 0
    assert args.provider == "codex"
    loaded = {name for name in sys.modules if name.startswith("cli.commands.")}
    assert loaded == {
        "cli.commands.agents",
        "cli.commands.agents.parsers",
        "cli.commands.agents.impersonation_parsers",
        "cli.commands.extensions",
        "cli.commands.extensions.parsers",
        "cli.commands.extensions.parsers.mcp",
        "cli.commands.extensions.parsers.packages",
        "cli.commands.extensions.parsers.plugins",
    }, loaded


def _import_boundary(entry: str) -> None:
    """`cli.commands` is an empty package door and `cli.parsers` stays Settings-free."""
    sys.meta_path.insert(0, _DenyConfig())
    import cli.commands

    assert not any(name.startswith("cli.commands.") for name in sys.modules)
    assert not hasattr(cli.commands, "__getattr__")
    assert not hasattr(cli.commands, "__all__")
    if entry == "parser":
        _parse_real_subcommands()
    elif entry == "config":
        import cli.commands.management.config  # pyright: ignore[reportUnusedImport]
    assert "base.config" not in sys.modules


class _DenySettings(importlib.abc.MetaPathFinder):
    admitted = False

    def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
        if not self.admitted and (fullname == "base.config" or fullname.startswith("base.config.")):
            raise AssertionError("premature runtime Settings import")


def _forbid_network() -> None:
    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("initialization performed a network dial")

    socket.socket.connect = no_network  # pyright: ignore[reportAttributeAccessIssue]
    socket.create_connection = no_network  # pyright: ignore[reportAttributeAccessIssue]


def _init_and_start_settings_free() -> None:
    """`init` and `start` stay settings-free until the home is admitted; delivery stays lazy."""
    import os
    from pathlib import Path

    sys.path.insert(0, sys.argv[2])

    guard = _DenySettings()
    sys.meta_path.insert(0, guard)
    _forbid_network()
    from base import cluster
    from cli import main, start_intent, startup_support

    home = Path(os.environ["AVA_HOME"])
    checkout = home.parent / "checkout"
    checkout.mkdir()
    start_intent.checkout = lambda: checkout
    cluster.port_free = lambda _port: True
    calls: list[str] = []

    def configured() -> None:
        assert (home / "start-intent.json").is_file()
        assert (home / ".env").is_file()
        assert not (home.parent / "clusters.json").exists()

    def log(args: list[str], *, initialize: object) -> None:
        assert args == ["start"]
        configured()
        calls.append("logging")

    startup_support.init_cli_logging = log

    def start(**kwargs: Any) -> int:
        assert not kwargs["command"].config.prepared
        configured()
        calls.append("runtime")
        return 0

    start_intent.run_start_command = start
    start_intent.complete_start = lambda **_kw: calls.append("boot-complete")
    start_intent.release_serving = lambda: calls.append("clear-serving")
    assert main.main(["start"]) == 1 and not home.exists()  # nothing to start yet, and nothing made
    assert (
        main.main(["init", "--serve-gateway", "--serve-agent-runner", "--machine-name", "probe"])
        == 0
    )
    configured()
    assert calls == []  # init started nothing
    assert "base.config" not in sys.modules
    guard.admitted = True
    from base.config import ConfigBoot

    def no_prepare(self):
        raise AssertionError("typed root prepared configuration before runtime delivery")

    ConfigBoot.prepare = no_prepare
    assert main.main(["start"]) == 0
    assert calls == ["logging", "runtime", "boot-complete"]
    assert "base.config" in sys.modules  # admitted root types are allowed; delivery stayed lazy


def _help_admission() -> None:
    """Help preserves the service database gate and the operator handle's admission."""
    from cli import main

    try:
        main.main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("help must exit")
    assert "base.config" not in sys.modules
    from base.cluster.dataplane.direct_url import ClusterDirectUrl
    from base.db import Database
    from base.db.code_version_gate import ProcessDbGate
    from cli.database import operator_database_factory
    from cli.database.runtime import CommandRuntime

    command = CommandRuntime.create()

    def unavailable() -> int:
        raise AssertionError("help and operator admission must not read a Git version")

    service = ProcessDbGate(version=unavailable, process="service")
    assert service.min_read_due() is True
    seen: list[ProcessDbGate] = []

    def from_settings(*, gate: ProcessDbGate, direct_url: ClusterDirectUrl) -> object:
        assert direct_url.local_host == command.machine.host
        assert gate.application_name() == "ava:cli"
        assert gate.min_read_due() is False
        seen.append(gate)
        return object()

    Database.from_settings = from_settings  # pyright: ignore[reportAttributeAccessIssue]
    factory = operator_database_factory(local_host=command.machine.host)
    assert not seen
    factory()
    factory()
    assert len(seen) == 2 and seen[0] is seen[1]
    assert service.min_read_due() is True


if __name__ == "__main__":
    if sys.argv[1] == "defer_logging":
        _defer_logging()
    elif sys.argv[1] == "import_boundary":
        _import_boundary(sys.argv[2])
    elif sys.argv[1] == "init_and_start_settings_free":
        _init_and_start_settings_free()
    elif sys.argv[1] == "help_admission":
        _help_admission()
    else:
        raise SystemExit(f"unknown scenario {sys.argv[1]!r}")

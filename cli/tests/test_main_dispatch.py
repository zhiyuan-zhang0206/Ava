"""`ava` CLI top-level dispatch routing.

main() builds the argparse parser, parses argv, then calls `args.func(args)`
where `func` was bound at parser-build time via `set_defaults(func=...)`,
referring to the `_h_*` handler defined in its own `cli.parsers.<domain>`
module. Each handler lazy-imports the cmd_X impl; this test patches the
handler binding (on the module that defines it, before the parser is built)
to record routing without invoking real cmd_start / cmd_cluster_status / etc.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from base.host.env.dotenv_boot import LAUNCHER_PROFILE_ENV_KEY
from base.native_process import code_version
from cli import main as _main
from cli.parsers import agents as _agents
from cli.parsers import backup as _backup
from cli.parsers import build_parser
from cli.parsers import cluster as _cluster
from cli.parsers import host as _host
from cli.parsers import logs as _logs
from cli.parsers import mcp as _mcp
from cli.parsers import plugins as _plugins
from cli.parsers import pty as _pty


def _subparser_children(action: argparse.Action) -> dict[str, argparse.ArgumentParser] | None:
    """The name -> parser map of a subparsers action, or None for an ordinary
    `--flag choices=(...)` action (whose `.choices` is a plain tuple/list of
    values, not a dict). Checked structurally (every choice value is itself a
    parser) rather than by `isinstance(action, argparse._SubParsersAction)` —
    that private generic narrows to an unparameterized `Unknown`, which makes
    `tests/cli`'s pyright tier (`reportUnknownMemberType = "error"`) fail on the
    resulting `.choices` access."""
    choices = action.choices
    if not isinstance(choices, dict) or not choices:
        return None
    typed = cast("dict[str, object]", choices)
    if not all(isinstance(v, argparse.ArgumentParser) for v in typed.values()):
        return None
    return cast("dict[str, argparse.ArgumentParser]", typed)


def _iter_leaf_parsers(
    parser: argparse.ArgumentParser,
) -> list[argparse.ArgumentParser]:
    """Every parser in the tree with no subcommands of its own — the ones a
    dispatch actually lands on (every `add_subparsers` call in cli/parsers/ is
    `required=True`, so a group parser itself never carries its own `func`)."""
    subparsers_actions = [
        children for action in parser._actions if (children := _subparser_children(action))
    ]
    if not subparsers_actions:
        return [parser]
    leaves: list[argparse.ArgumentParser] = []
    seen: set[int] = set()
    for children in subparsers_actions:
        for child in children.values():
            if id(child) in seen:
                continue
            seen.add(id(child))
            leaves.extend(_iter_leaf_parsers(child))
    return leaves


def test_every_leaf_subcommand_binds_a_handler_from_its_parser_module() -> None:
    """Every leaf subcommand's `func` must be a callable defined in the
    `cli.parsers.*` module that built it — guards against a builder left
    pointing at a handler name that was renamed or removed (e.g. a stale
    `cli.main` re-export)."""
    leaves = _iter_leaf_parsers(build_parser())
    assert len(leaves) > 100, "sanity: the walk should reach the whole command surface"
    for leaf in leaves:
        func = leaf.get_default("func")
        assert callable(func), f"{leaf.prog!r} has no callable 'func' bound"
        module = getattr(func, "__module__", "")
        assert module.startswith("cli.parsers."), (
            f"{leaf.prog!r} binds {func!r} from {module!r}, not a cli.parsers module"
        )


def test_import_defers_cli_logging_until_dispatch(tmp_path: Path) -> None:
    """A settings-free entry can import the parser before choosing its path."""
    code = """
import sys
import cli.main
assert 'base.log' not in sys.modules
assert 'base.config' not in sys.modules
"""
    result = subprocess.run(  # noqa: S603 - fixed interpreter and literal probe.
        [sys.executable, "-B", "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# Each top-level (and nested) ava sub-command maps to a _h_* handler defined
# in its own cli.parsers.<domain> module.
_HANDLERS: tuple[tuple[list[str], object, str], ...] = (
    (["stop"], _host, "_h_stop"),
    (["restart"], _host, "_h_restart"),
    (["status"], _host, "_h_status"),
    (["pty", "freeze", "--holder", "operator", "--reason", "cleanup"], _pty, "_h_pty_freeze"),
    (["pty", "status"], _pty, "_h_pty_status"),
    (["pty", "resume", "generation"], _pty, "_h_pty_resume"),
    (["converge"], _host, "_h_converge"),
    (["firewall", "status"], _host, "_h_firewall_status"),
    (["firewall", "sync"], _host, "_h_firewall_sync"),
    (["cluster", "status"], _cluster, "_h_cluster_status"),
    (["plugins", "update"], _plugins, "_h_plugins_update"),
    (["agents", "ls"], _agents, "_h_agents_ls"),
    (["agents", "cancel", "1"], _agents, "_h_agents_cancel"),
    (["agents", "restart", "1"], _agents, "_h_agents_restart"),
    (["agents", "terminate", "1"], _agents, "_h_agents_terminate"),
    (["agents", "kill", "1"], _agents, "_h_agents_kill"),
    (["agents", "resurrect", "1"], _agents, "_h_agents_resurrect"),
    (["agents", "send", "1", "hi", "--source", "user"], _agents, "_h_agents_send"),
    (["mcp", "serve"], _mcp, "_h_mcp_serve"),
    (["memory", "search", "context"], _mcp, "_h_memory_search"),
    (["logs", "retention"], _logs, "_h_logs_retention"),
    (["logs", "rotate"], _logs, "_h_logs_rotate"),
)


@pytest.mark.parametrize(("argv", "module", "handler_name"), _HANDLERS)
def test_dispatch_invokes_per_subcommand_handler(
    argv: list[str], module: object, handler_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each sub-command routes to its `_h_*` handler with the parsed Namespace."""
    captured: dict[str, argparse.Namespace] = {}

    def _fake(args: argparse.Namespace) -> int:
        captured["args"] = args
        return 42

    monkeypatch.setattr(module, handler_name, _fake)
    rc = _main.main(argv)
    assert rc == 42
    assert "args" in captured


def test_cli_discards_an_inherited_process_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI always constructs the full settings domain set."""
    monkeypatch.setenv("AVA_PROCESS_PROFILE", "agent")
    assert "AVA_PROCESS_PROFILE" in os.environ
    # main() records the discarded profile as AVA_LAUNCHER_PROFILE. Register the absent key
    # (setenv) before removing it so teardown deletes what main() assigned.
    monkeypatch.setenv(LAUNCHER_PROFILE_ENV_KEY, "")
    monkeypatch.delenv(LAUNCHER_PROFILE_ENV_KEY)

    def _fake(_args: argparse.Namespace) -> int:
        return 0

    monkeypatch.setattr(_host, "_h_status", _fake)

    assert _main.main(["status"]) == 0
    assert "AVA_PROCESS_PROFILE" not in os.environ


def test_status_handler_body_forwards_the_parsed_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The routing test above stubs the `_h_*` handler itself, so no handler body
    ever runs under test — a handler reading a flag the parser no longer defines
    (`args.via_gateway` after `--via-gateway` was dropped) stays green here and
    AttributeErrors on the first real `ava status`. Stub one level lower instead:
    `cmd_status`, which `_h_status` lazy-imports, so the real body executes against
    the real Namespace."""
    import cli.commands.lifecycle.status as _status_commands

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(_status_commands, "cmd_status", lambda **kwargs: calls.append(kwargs) or 0)  # pyright: ignore[reportUnknownArgumentType]

    assert _main.main(["status"]) == 0
    assert calls == [{}]


def test_restart_handler_forwards_the_parsed_config_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restart parser keeps the JSON string intact for its HTTP command."""
    from cli.commands.agents import control as agents_commands

    calls: list[tuple[int, str | None, str | None]] = []

    def restart(agent_id: int, config_json: str | None = None, source: str | None = None) -> int:
        calls.append((agent_id, config_json, source))
        return 0

    monkeypatch.setattr(
        agents_commands,
        "cmd_agents_restart",
        restart,
    )

    assert _main.main(["agents", "restart", "8", "--config", '{"llm_model":"gpt-5.6-sol"}']) == 0
    assert calls == [(8, '{"llm_model":"gpt-5.6-sol"}', None)]


def test_migrations_subcommand_removed() -> None:
    """`ava migrations apply` is gone — migration is now a side-effect of
    `ava start`. argparse exits 2 on the unknown subcommand."""
    with pytest.raises(SystemExit):
        _main._build_parser().parse_args(["migrations", "apply"])


@pytest.mark.parametrize(
    "argv",
    [
        ["cluster", "update", "--prepared", "/private/request.json"],
        ["cluster", "release", "status"],
        ["cluster", "pitr", "activate"],
    ],
)
def test_image_release_and_pitr_activation_verbs_are_removed(argv: list[str]) -> None:
    """The retained-image release and PITR-activation operator verbs no longer parse."""
    with pytest.raises(SystemExit) as exited:
        _main._build_parser().parse_args(argv)

    assert exited.value.code == 2


def test_logs_retention_parser_accepts_the_public_flags() -> None:
    """The local-log cleanup contract is reachable at `ava logs retention`."""
    args = _main._build_parser().parse_args(
        ["logs", "retention", "--family-days", "gateway=31,ops=30", "--dry-run"]
    )

    assert args.family_days == {"gateway": 31, "ops": 30}
    assert args.dry_run is True


def test_logs_retention_parser_accepts_default_as_the_other_family() -> None:
    args = _main._build_parser().parse_args(
        ["logs", "retention", "--family-days", "agent=15,default=14"]
    )

    assert args.family_days == {"agent": 15, "other": 14}


def test_logs_retention_parser_rejects_combined_age_modes() -> None:
    with pytest.raises(SystemExit):
        _main._build_parser().parse_args(
            [
                "logs",
                "retention",
                "--older-than",
                "21",
                "--family-days",
                "agent=15",
            ]
        )


def test_logs_retention_parser_rejects_unknown_family() -> None:
    with pytest.raises(SystemExit):
        _main._build_parser().parse_args(["logs", "retention", "--family-days", "restarter=4"])


def test_logs_retention_default_comes_from_observability_settings() -> None:
    from base.config.observability import ObservabilitySettings

    field = ObservabilitySettings.model_fields["log_retention_days"]
    configured = ObservabilitySettings(AVA_LOG_RETENTION_DAYS=23)

    assert field.alias == "AVA_LOG_RETENTION_DAYS"
    assert configured.log_retention_days == 23


def test_logs_retention_parser_rejects_non_positive_days() -> None:
    with pytest.raises(SystemExit):
        _main._build_parser().parse_args(["logs", "retention", "--older-than", "0"])


def test_logs_retention_settings_reject_non_positive_environment_default() -> None:
    from pydantic import ValidationError

    from base.config.observability import ObservabilitySettings

    with pytest.raises(ValidationError):
        ObservabilitySettings(AVA_LOG_RETENTION_DAYS=0)


def test_logs_retention_help_explains_defaults_and_dry_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        _main._build_parser().parse_args(["logs", "retention", "--help"])

    assert exited.value.code == 0
    help_text = capsys.readouterr().out
    assert "14 days" in help_text
    assert "AVA_LOG_RETENTION_DAYS" in help_text
    assert "agent=15" in help_text
    assert "--older-than DAYS | --family-days" in help_text
    assert "without\n                        deleting" in help_text


def test_backup_operations_parser_binds_status_and_retire() -> None:
    parser = _main._build_parser()
    status = parser.parse_args(["backup", "operations", "status"])
    retire = parser.parse_args(["backup", "operations", "retire"])
    confirmed = parser.parse_args(["backup", "operations", "retire", "--confirm"])
    assert status.func is _backup._h_backup_operations_status
    assert retire.func is _backup._h_backup_operations_retire
    assert (retire.confirm, confirmed.confirm) == (False, True)


@pytest.mark.parametrize(
    "argv",
    [
        ["pitr", "operations", "status"],
        ["pitr", "retention", "inspect"],
        ["pitr", "drill"],
        ["pitr", "multipart", "list"],
        ["pitr", "snapshot", "verify"],
    ],
)
def test_the_removed_wal_verbs_no_longer_parse(argv: list[str]) -> None:
    """The self-written PITR stack is gone; operation custody is `ava backup operations`."""
    with pytest.raises(SystemExit) as exited:
        _main._build_parser().parse_args(argv)

    assert exited.value.code == 2


def test_start_subcommand_forwards_argparse_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ava start --only-service gateway ...` reaches _h_start with the parsed
    argparse Namespace: the service selection is all `start` takes."""
    captured: dict[str, argparse.Namespace] = {}

    def _fake(args: argparse.Namespace) -> int:
        captured["args"] = args
        return 7

    monkeypatch.setattr(_host, "_h_start", _fake)
    # `start` is the one verb with a pre-dispatch side effect (the settings-free
    # installed-home gate), which this test neutralizes — it asserts flag
    # forwarding, not bring-up behaviour.
    rc = _main.main(["start", "--only-service", "gateway", "--only-service", "frontend"])
    assert rc == 7
    ns = captured["args"]
    assert ns.only_service == ["gateway", "frontend"]
    assert ns.disable_service == [] and ns.all_services is False and ns.persist_services is True


def test_init_subcommand_binds_its_handler_and_takes_the_identity_flags() -> None:
    """`ava init` owns the first-start inputs: machine name, capabilities, gateway."""
    args = _main._build_parser().parse_args(
        [
            "init",
            "--machine-name",
            "mac",
            "--serve-gateway",
            "--memory-remote",
            "git@x:y.git",
            "--gateway-url",
            "https://ava.example.com",
        ]
    )
    assert args.func is _host._h_init
    assert args.machine_name == "mac"
    assert args.serve_gateway is True
    assert args.serve_agent_runner is None  # unset: no flag given
    assert args.memory_remote == "git@x:y.git"
    assert args.gateway_url == "https://ava.example.com"


def test_maintenance_verbs_opt_out_of_the_gateway_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`status` / `cluster` / `agents` / `config` / `logs` set AVA_CONFIG_FETCH=skip
    before dispatch (settings-lite: they must work while the gateway is down);
    start and normal pause/stop need the real cluster configuration."""
    import os as _os

    # Blanking os.environ drops AVA_HOME, which would make the home `~/.ava` —
    # and on a host that runs a cluster its own checkout gate would refuse `stop`
    # from this checkout. Name a home with no `source` of its own: this test
    # asserts env pinning, not gate behaviour.
    for verb in ("status", "cluster", "agents", "config", "logs"):
        env = {"PATH": "/usr/bin", "AVA_HOME": str(tmp_path)}
        monkeypatch.setattr(_os, "environ", env)
        monkeypatch.setattr(_main, "_build_parser", lambda v=verb: _noop_parser(v))
        assert _main.main([verb]) == 0
        assert env.get("AVA_CONFIG_FETCH") == "skip", f"{verb} must be settings-lite"

    # Starting and graceful draining both need data-plane configuration.
    for verb in ("start", "pause", "stop"):
        env = {"PATH": "/usr/bin", "AVA_HOME": str(tmp_path)}
        monkeypatch.setattr(_os, "environ", env)
        monkeypatch.setattr(_main, "_build_parser", lambda v=verb: _noop_parser(v))
        assert _main.main([verb]) == 0
        assert "AVA_CONFIG_FETCH" not in env


def _noop_parser(verb: str) -> argparse.ArgumentParser:
    """A parser that accepts `verb` as a positional and dispatches to a no-op —
    for tests that only assert main()'s pre-dispatch env pinning."""
    parser = argparse.ArgumentParser(prog="ava")
    parser.add_argument("verb", nargs="?")
    parser.set_defaults(func=lambda _args: 0)
    return parser


def test_settings_load_failure_prints_env_template(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When a handler raises ValidationError (fresh host, no .env), main()
    prints a copy-paste env template instead of a raw traceback."""
    from pydantic import ValidationError

    err = ValidationError.from_exception_data(
        "Settings",
        [{"type": "missing", "loc": ("db_url",), "input": None}],  # type: ignore[list-item]  # pyright: ignore[reportArgumentType]
    )

    def _boom(_args: argparse.Namespace) -> int:
        raise err

    monkeypatch.setattr(_host, "_h_status", _boom)
    rc = _main.main(["status"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "AVA_DB_URL" in captured.err


# -- checkout gate: every command from a foreign checkout -----------------------


def _owned_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home that carries its own `source` checkout, named by AVA_HOME. This
    checkout is not it, so the gate must refuse every command."""
    home = tmp_path / ".ava"
    (home / "source").mkdir(parents=True)
    monkeypatch.setenv("AVA_HOME", str(home))
    return home


@pytest.mark.parametrize(
    "argv",
    [
        ["start"],
        ["stop", "-y"],
        ["pause"],
        ["restart"],
        ["converge"],
        ["maintenance", "stop"],
        ["cluster", "destroy"],
        ["config", "set", "KEY=VALUE"],
        ["logs", "retention"],
        ["agents", "send", "1", "hello"],
        ["boot"],
        ["status"],
        ["agents", "ls"],
        ["config", "get"],
        ["stop", "--help"],
        ["config", "unset", "--", "--help"],
        ["boot", "--help"],
    ],
)
def test_foreign_checkout_is_refused_before_dispatch(
    argv: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A checkout that is not the home's own does not operate that home: every
    command is refused before anything dispatches (and before `ava boot` could
    retry a start), and nothing is written to the home."""
    home = _owned_home(tmp_path, monkeypatch)
    dispatched: list[str] = []
    monkeypatch.setattr(_main, "_build_parser", lambda: _noop_parser_recording(argv[0], dispatched))

    rc = _main.main(argv)

    assert rc == 1
    assert dispatched == [], "the handler must never run"
    err = capsys.readouterr().err
    assert str(home) in err, "the message must name the home it acts on"
    assert str(home / "source") in err, "and the checkout that may act on it"
    assert "AVA_HOME" in err
    assert sorted(path.name for path in home.iterdir()) == ["source"], "nothing may be written"


def test_the_homes_own_checkout_runs_the_state_changing_verbs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The home's `source` IS this checkout (here: a link to it), so its own CLI
    stops its own cluster — the production path."""
    home = tmp_path / ".ava"
    home.mkdir()
    (home / "source").symlink_to(Path(_main.__file__).resolve().parents[1])
    monkeypatch.setenv("AVA_HOME", str(home))
    dispatched: list[str] = []
    monkeypatch.setattr(_main, "_build_parser", lambda: _noop_parser_recording("stop", dispatched))

    assert _main.main(["stop", "-y"]) == 0
    assert dispatched == ["stop"]


def test_a_home_with_no_source_accepts_any_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test or scratch home has no checkout of its own: any checkout may
    start, stop and reconfigure it."""
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    dispatched: list[str] = []
    monkeypatch.setattr(_main, "_build_parser", lambda: _noop_parser_recording("stop", dispatched))

    assert _main.main(["stop", "-y"]) == 0
    assert dispatched == ["stop"]


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_foreign_checkout_may_still_ask_for_a_lone_help_flag(
    flag: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A lone `-h`/`--help` reaches argparse (which prints and exits 0) rather
    than the gate: no verb runs."""
    _owned_home(tmp_path, monkeypatch)
    dispatched: list[str] = []
    monkeypatch.setattr(_main, "_build_parser", lambda: _noop_parser_recording("ava", dispatched))

    with pytest.raises(SystemExit) as exc:
        _main.main([flag])

    assert exc.value.code == 0
    assert "source checkout" not in capsys.readouterr().err


def test_foreign_checkout_may_still_run_bare_ava(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no argv the gate has no verb to refuse; argparse owns the usage error."""
    _owned_home(tmp_path, monkeypatch)
    dispatched: list[str] = []
    monkeypatch.setattr(_main, "_build_parser", lambda: _noop_parser_recording("ava", dispatched))

    assert _main.main([]) == 0

    assert dispatched == ["ava"]
    assert "source checkout" not in capsys.readouterr().err


def _noop_parser_recording(verb: str, sink: list[str]) -> argparse.ArgumentParser:
    """`_noop_parser` that records that dispatch actually happened. Permissive
    enough to swallow the real verbs' flags (`-y`, `--path`) without argparse
    erroring before the assertion under test."""
    parser = argparse.ArgumentParser(prog="ava")
    parser.add_argument("verb", nargs="?")
    parser.add_argument("rest", nargs="*")
    parser.add_argument("-y", action="store_true")
    parser.add_argument("--path")
    parser.set_defaults(func=lambda _args: sink.append(verb) or 0)
    return parser


def test_init_and_start_are_settings_free_until_the_home_is_admitted(tmp_path: Path) -> None:
    """Deny settings and network; the real public dispatch of `init`, then `start`,
    must reach its runtime boundary."""
    repo = Path(__file__).resolve().parents[2]
    code = r"""
import importlib.abc
import os
import socket
import sys
import types
from pathlib import Path
sys.path.insert(0, sys.argv[1])
class DenySettings(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "base.config" or fullname.startswith("base.config."):
            raise AssertionError("premature runtime Settings import")
sys.meta_path.insert(0, DenySettings())
def no_network(*args, **kwargs):
    raise AssertionError("initialization performed a network dial")
socket.socket.connect = no_network
socket.create_connection = no_network
from cli import main, start_intent
from base import cluster
home = Path(os.environ["AVA_HOME"])
checkout = home.parent / "checkout"
checkout.mkdir()
start_intent._checkout = lambda: checkout
cluster.port_free = lambda _port: True
calls = []
def configured():
    assert (home / "start-intent.json").is_file()
    assert (home / ".env").is_file()
    assert not (home.parent / "clusters.json").exists()
def log(args):
    assert args == ["start"]
    configured()
    calls.append("logging")
main._init_cli_logging = log
def start(**kwargs):
    configured()
    calls.append("runtime")
    return 0
sys.modules["cli.commands.lifecycle.start"] = types.SimpleNamespace(cmd_start=start)
sys.modules["cli.commands.lifecycle.root_driver"] = types.SimpleNamespace(
    complete_boot_start=lambda: calls.append("boot-complete")
)
sys.modules["base.deploy.lifecycle.start_serving"] = types.SimpleNamespace(
    clear_serving=lambda: calls.append("clear-serving")
)
assert main.main(["start"]) == 1 and not home.exists()  # nothing to start yet, and nothing made
assert main.main(["init", "--serve-gateway", "--serve-agent-runner", "--machine-name", "probe"]) == 0
configured()
assert calls == []  # init started nothing
assert main.main(["start"]) == 0
assert calls == ["logging", "runtime", "boot-complete"]
assert "base.config" not in sys.modules
"""
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env.update(
        AVA_HOME=str(tmp_path / "home"),
        AVA_DB_URL="postgresql://foreign.invalid/forbidden",
        AVA_GATEWAY_URL="http://foreign.invalid",
    )
    result = subprocess.run(  # noqa: S603 — fixed interpreter and literal probe
        [sys.executable, "-I", "-B", "-c", code, str(repo)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("entry", ["package", "parser", "config"])
def test_command_import_boundary_is_settings_free(entry: str, tmp_path: Path) -> None:
    """`cli.commands` is an empty package door: importing it does no import work
    of its own, so it must load no `cli.commands.*` submodule and pull in no
    `base.config` — Settings stays out of the boundary. The `parser` case
    additionally builds the real argparse tree and parses a real subcommand's
    args (without dispatching to its handler), since `cli.parsers` must stay
    just as settings-free while doing that. The `config` case imports the
    `ava config` module, whose local repair path must also run without Settings."""
    code = """
import importlib.abc
import sys
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'base.config':
            raise AssertionError('forbidden early import: ' + fullname)
sys.meta_path.insert(0, Deny())
import cli.commands
assert not any(name.startswith('cli.commands.') for name in sys.modules)
assert not hasattr(cli.commands, '__getattr__')
assert not hasattr(cli.commands, '__all__')
if sys.argv[1] == 'parser':
    from cli.parsers import build_parser
    parser = build_parser()
    args = parser.parse_args(['cluster', 'db-authority', 'issue-unit', '--machine', 'unit', '--home', '/unit', '--out', '/unused/bundle'])
    assert args.machine == 'unit'
elif sys.argv[1] == 'config':
    import cli.commands.management.config
assert 'base.config' not in sys.modules
"""
    result = subprocess.run(  # noqa: S603 — fixed interpreter, isolated import-only program.
        [sys.executable, "-B", "-c", code, entry],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_main_declares_the_cli_exempt_from_the_database_code_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ava stop` writes to the database to drain agents, so a host left on stale
    code must still be able to run it: the CLI entry point exempts itself first."""
    monkeypatch.setattr(code_version, "_db_gate_exempt", False)
    with pytest.raises(SystemExit):
        _main.main(["--help"])
    assert code_version.db_gate_applies() is False

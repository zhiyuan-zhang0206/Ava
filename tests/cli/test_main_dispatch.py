"""`ava` CLI top-level dispatch routing.

main() builds the argparse parser, parses argv, then calls `args.func(args)`
where `func` was bound at parser-build time via `set_defaults(func=...)`.
Each per-command handler `_h_*` in cli.main lazy-imports the cmd_X impl;
this test patches the handler binding to record routing without invoking
real cmd_start / cmd_cluster_status / etc.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cli import main as _main


def test_import_defers_detached_cli_logging_until_dispatch(tmp_path: Path) -> None:
    """A settings-free entry can import the parser before choosing its path."""
    code = """
import sys
import cli.main
assert 'shared.log' not in sys.modules
assert 'shared.config' not in sys.modules
"""
    result = subprocess.run(  # noqa: S603 - fixed interpreter and literal probe.
        [sys.executable, "-B", "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "HOME": str(tmp_path), "AVA_CLI_LOG_NAME": "retained-entry-test"},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("boot_mode", ["lite", "eager"])
@pytest.mark.parametrize("direction", ["candidate", "previous"])
def test_release_stage_prepares_identity_before_loading_config(
    tmp_path: Path, boot_mode: str, direction: str
) -> None:
    """Exercise the cold stage/boot/start chain, stopping at service effects."""
    repo = Path(__file__).resolve().parents[2]
    code = r"""
import json
import os
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
sys.path.insert(0, sys.argv[1])
from cli import main, start_intent, start_runtime
from cli.parsers import build_parser
from cli.release_transition import stage
from cli.release_transition.journal import Operation
from cli.release_transition.request import ReleaseRef, Request
from shared import cluster
from shared.maintenance_state import MaintenanceHold
from shared.start_inputs import configuration_digest

home = Path(os.environ["AVA_HOME"])
checkout = home.parent / "checkout"
checkout.mkdir()
start_intent._checkout = lambda: checkout
cluster._port_free = lambda _port: True
start_intent.prepare_start(build_parser().parse_args(["start", "--worktree"]))
before = (home / ".env").read_bytes()
reference = ReleaseRef(artifact_digest="a"*64, manifest_digest="b"*64,
    schema_digest="c"*64, source_commit="d"*40)
request = Request(id=uuid4(), home=str(home),
    registry=os.environ["AVA_CLUSTER_REGISTRY"], created_at=datetime.now(UTC),
    platform_tag="cold-start-test", machine="test",
    previous=reference.model_copy(update={"artifact_digest":"e"*64}),
    candidate=reference, executor=reference,
    configuration_digest=configuration_digest(home))
operation = Operation(request=request, phase="starting", direction=sys.argv[2])
request.path.parent.mkdir(parents=True)
request.path.write_text(operation.model_dump_json())
(home / "updates/active").write_text(str(request.path))
pause = home / "run/deploy-pause-owner.json"
pause.parent.mkdir()
pause.write_text(json.dumps({"state":"paused", "holder":str(request.id),
    "acquired_at":request.created_at.isoformat(),
    "maintenance":MaintenanceHold(phase="starting").encode()}))

# Only native-manager placement and installed-image bytes are fixtures.
# Stage, boot, identity preparation, config boot, and pause authorization are real.
from shared import os_boot_unit
os_boot_unit.in_boot_unit = lambda value: value == home
ReleaseRef.verify = lambda self, *args: self
start_runtime.admit_release = lambda *args, **kwargs: start_runtime.StartRuntime.development(checkout)
prepare = start_intent._prepare_start_locked
def prepared(*args):
    assert "shared.config" not in sys.modules, "configuration loaded before identity"
    assert "shared.dotenv_boot" not in sys.modules, "home resolved before identity"
    prepare(*args)
start_intent._prepare_start_locked = prepared
main._init_detached_cli_logging = lambda: None
commands = types.ModuleType("cli.commands.start")
sys.modules["cli.commands.start"] = commands
calls = []
def effects(**kwargs):
    from cli.commands._pause_resume import resume_after_start
    from shared import maintenance, start_serving
    from shared.config import get_field
    from dotenv import dotenv_values
    expected = dotenv_values(home / ".env")
    @resume_after_start
    def start():
        assert maintenance.start_authorized()
        assert get_field("machine_serve_gateway") is True
        assert get_field("machine_serve_agent_runner") is True
        assert get_field("machine_serve_observability_station") is False
        for key in ("AVA_DB_URL", "AVA_REDIS_URL", "AVA_GATEWAY_PORT"):
            assert os.environ[key] == expected[key], key
        calls.append("start")
        return 0
    start_serving.is_serving = lambda: True
    return start()
commands.cmd_start = effects
sys.modules["cli.commands._root_driver"] = types.SimpleNamespace(complete_boot_start=lambda: None)
assert stage.start_operation(request.path) == 0
assert calls == ["start"]
assert json.loads(pause.read_text())["state"] == "paused"
assert (home / ".env").read_bytes() == before
"""
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env.update(
        AVA_HOME=str(tmp_path.resolve() / "home"),
        AVA_HOME_OVERRIDE="1",
        AVA_CLUSTER_REGISTRY=str(tmp_path.resolve() / "clusters.json"),
        AVA_DB_URL="postgresql://foreign.invalid/forbidden",
        AVA_GATEWAY_URL="http://foreign.invalid",
        AVA_MACHINE_SERVE_GATEWAY="false",
    )
    if boot_mode == "eager":
        env["AVA_CONFIG_BOOT"] = boot_mode
    result = subprocess.run(  # noqa: S603 — fixed interpreter and literal probe
        [sys.executable, "-I", "-B", "-c", code, str(repo), direction],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# Each top-level (and nested) ava sub-command maps to a _h_* handler in cli.main.
_HANDLERS: tuple[tuple[list[str], str], ...] = (
    (["stop"], "_h_stop"),
    (["restart"], "_h_restart"),
    (["status"], "_h_status"),
    (["pty", "freeze", "--holder", "operator", "--reason", "cleanup"], "_h_pty_freeze"),
    (["pty", "status"], "_h_pty_status"),
    (["pty", "resume", "generation"], "_h_pty_resume"),
    (["cluster", "update", "--prepared", "/private/request.json"], "_h_cluster_update"),
    (["converge"], "_h_converge"),
    (["firewall", "status"], "_h_firewall_status"),
    (["firewall", "sync"], "_h_firewall_sync"),
    (["cluster", "status"], "_h_cluster_status"),
    # The pre-#217 name stays as an alias — both spellings route to the same
    # handler (issue #217: the verb provisions the ava_runner POSTGRES role,
    # not a machine capability).
    (["plugins", "update"], "_h_plugins_update"),
    (["agents", "ls"], "_h_agents_ls"),
    (["agents", "cancel", "1"], "_h_agents_cancel"),
    (["agents", "restart", "1"], "_h_agents_restart"),
    (["agents", "terminate", "1"], "_h_agents_terminate"),
    (["agents", "kill", "1"], "_h_agents_kill"),
    (["agents", "resurrect", "1"], "_h_agents_resurrect"),
    (["agents", "send", "1", "hi", "--source", "user"], "_h_agents_send"),
    (["mcp", "serve"], "_h_mcp_serve"),
    (["memory", "search", "context"], "_h_memory_search"),
    (["logs", "retention"], "_h_logs_retention"),
    (["logs", "rotate"], "_h_logs_rotate"),
)


@pytest.mark.parametrize(("argv", "handler_name"), _HANDLERS)
def test_dispatch_invokes_per_subcommand_handler(
    argv: list[str], handler_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each sub-command routes to its `_h_*` handler with the parsed Namespace."""
    captured: dict[str, argparse.Namespace] = {}

    def _fake(args: argparse.Namespace) -> int:
        captured["args"] = args
        return 42

    monkeypatch.setattr(_main, handler_name, _fake)
    rc = _main.main(argv)
    assert rc == 42
    assert "args" in captured


def test_cli_discards_an_inherited_process_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI always constructs the full settings domain set."""
    monkeypatch.setenv("AVA_PROCESS_PROFILE", "agent")
    assert "AVA_PROCESS_PROFILE" in os.environ

    def _fake(_args: argparse.Namespace) -> int:
        return 0

    monkeypatch.setattr(_main, "_h_status", _fake)

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
    import cli.commands.status as _status_commands

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(_status_commands, "cmd_status", lambda **kwargs: calls.append(kwargs) or 0)  # pyright: ignore[reportUnknownArgumentType]

    assert _main.main(["status"]) == 0
    assert calls == [{}]


def test_restart_handler_forwards_the_parsed_config_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restart parser keeps the JSON string intact for its HTTP command."""
    from cli.commands import agents as agents_commands

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


def test_cluster_update_requires_a_captured_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No implicit mutable-checkout or moving-main update remains in the CLI."""
    with pytest.raises(SystemExit) as exited:
        _main._build_parser().parse_args(["cluster", "update"])

    assert exited.value.code == 2
    assert "--prepared" in capsys.readouterr().err


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
    from shared.config.observability import ObservabilitySettings

    field = ObservabilitySettings.model_fields["log_retention_days"]
    configured = ObservabilitySettings(AVA_LOG_RETENTION_DAYS=23)

    assert field.alias == "AVA_LOG_RETENTION_DAYS"
    assert configured.log_retention_days == 23


def test_logs_retention_parser_rejects_non_positive_days() -> None:
    with pytest.raises(SystemExit):
        _main._build_parser().parse_args(["logs", "retention", "--older-than", "0"])


def test_logs_retention_settings_reject_non_positive_environment_default() -> None:
    from pydantic import ValidationError

    from shared.config.observability import ObservabilitySettings

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


def test_pitr_retention_inspect_parser_binds_read_only_handler() -> None:
    args = _main._build_parser().parse_args(["pitr", "retention", "inspect"])
    assert args.func is _main._h_pitr_retention_inspect


def test_start_subcommand_forwards_argparse_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ava start --machine-name foo --serve-gateway ...` reaches _h_start with
    the parsed argparse Namespace."""
    captured: dict[str, argparse.Namespace] = {}

    def _fake(args: argparse.Namespace) -> int:
        captured["args"] = args
        return 7

    monkeypatch.setattr(_main, "_h_start", _fake)
    # `start` is the one verb with a pre-dispatch side effect (the settings-free
    # installed-home gate), which this test neutralizes — it asserts flag
    # forwarding, not bring-up behaviour.
    rc = _main.main(
        [
            "start",
            "--machine-name",
            "mac",
            "--serve-gateway",
            "--memory-remote",
            "git@x:y.git",
            "--gateway-url",
            "https://ava.example.com",
        ]
    )
    assert rc == 7
    ns = captured["args"]
    assert ns.machine_name == "mac"
    assert ns.serve_gateway is True
    assert ns.serve_agent_runner is None  # unset -> falls back to file
    assert ns.memory_remote == "git@x:y.git"
    assert ns.gateway_url == "https://ava.example.com"


def test_maintenance_verbs_opt_out_of_the_gateway_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`status` / `cluster` / `agents` / `config` / `logs` set AVA_CONFIG_FETCH=skip
    before dispatch (settings-lite: they must work while the gateway is down);
    start and normal pause/stop need the real cluster configuration."""
    import os as _os

    import cli.preflight as _preflight

    # Blanking os.environ drops AVA_HOME, which makes this checkout read as
    # unanchored — so `stop` would hit the anchored-home gate. Neutralize it the
    # same way the parser handlers are stubbed below: this test asserts env
    # pinning, not gate behaviour.
    monkeypatch.setattr(_preflight, "require_anchored_home", lambda _verb: None)  # pyright: ignore[reportUnknownArgumentType]
    for verb in ("status", "cluster", "agents", "config", "logs"):
        env = {"PATH": "/usr/bin"}
        monkeypatch.setattr(_os, "environ", env)
        monkeypatch.setattr(_main, "_build_parser", lambda v=verb: _noop_parser(v))
        assert _main.main([verb]) == 0
        assert env.get("AVA_CONFIG_FETCH") == "skip", f"{verb} must be settings-lite"

    # Starting and graceful draining both need data-plane configuration.
    for verb in ("start", "pause", "stop"):
        env = {"PATH": "/usr/bin"}
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

    monkeypatch.setattr(_main, "_h_status", _boom)
    rc = _main.main(["status"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "AVA_DB_URL" in captured.err


# -- anchored-home gate on the destructive verbs -------------------------------


def _unanchored(monkeypatch: pytest.MonkeyPatch, home: str = "/Users/x/.ava") -> None:
    """Make this process read as a checkout that claims no cluster — the shape
    `resolve_ava_home` resolves to the DEFAULT home (production) with
    anchored=False."""

    import shared.dotenv_boot as _boot

    monkeypatch.setattr(_boot, "resolve_ava_home", lambda: (Path(home), False))


def _anchored(monkeypatch: pytest.MonkeyPatch, home: str = "/Users/x/.ava-worktree") -> None:

    import shared.dotenv_boot as _boot

    monkeypatch.setattr(_boot, "resolve_ava_home", lambda: (Path(home), True))


@pytest.mark.parametrize(
    "argv",
    [
        ["stop", "-y"],
        ["restart"],
        ["converge"],
        ["cluster", "recover"],
        ["logs", "retention"],
    ],
)
def test_unanchored_checkout_is_refused_before_dispatch(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dev worktree that never ran the install resolves to the DEFAULT home, so
    these verbs would act on production. They must refuse instead of dispatching."""
    _unanchored(monkeypatch)
    dispatched: list[str] = []
    monkeypatch.setattr(_main, "_build_parser", lambda: _noop_parser_recording(argv[0], dispatched))

    rc = _main.main(argv)

    assert rc == 1
    assert dispatched == [], "the handler must never run"
    err = capsys.readouterr().err
    assert "/Users/x/.ava" in err, "the message must name the home it would have hit"
    assert "ava start --worktree" in err


@pytest.mark.parametrize(
    "argv",
    [
        ["status"],
        ["cluster", "ls"],
        ["cluster", "status"],
        ["cluster", "down", "--path", "/somewhere"],
        ["cluster", "destroy", "--path", "/somewhere"],
        ["agents"],
    ],
)
def test_unanchored_checkout_still_runs_the_ungated_verbs(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-only verbs, and the two that name their target with --path, never act
    on the current home — gating them would break addressing another cluster."""
    _unanchored(monkeypatch)
    dispatched: list[str] = []
    monkeypatch.setattr(_main, "_build_parser", lambda: _noop_parser_recording(argv[0], dispatched))

    assert _main.main(argv) == 0
    assert dispatched == [argv[0]]


def test_anchored_checkout_runs_the_gated_verbs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate is about anchoring, nothing else: prod's own `ava` (and any
    installed worktree) still stops its own cluster."""
    _anchored(monkeypatch)
    dispatched: list[str] = []
    monkeypatch.setattr(_main, "_build_parser", lambda: _noop_parser_recording("stop", dispatched))

    assert _main.main(["stop", "-y"]) == 0
    assert dispatched == ["stop"]


def test_help_is_never_gated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ava stop --help` is a parse-only invocation: it must reach argparse (which
    prints help and exits 0) rather than be refused by the gate."""
    _unanchored(monkeypatch)
    dispatched: list[str] = []
    monkeypatch.setattr(_main, "_build_parser", lambda: _noop_parser_recording("stop", dispatched))

    with pytest.raises(SystemExit) as exc:
        _main.main(["stop", "--help"])

    assert exc.value.code == 0
    assert "claims no cluster" not in capsys.readouterr().err


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


def test_first_start_is_settings_free_until_identity_is_published(tmp_path: Path) -> None:
    """Deny settings and network; the real public dispatch must reach its runtime boundary."""
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
        if fullname == "shared.config" or fullname.startswith("shared.config."):
            raise AssertionError("premature runtime Settings import")
sys.meta_path.insert(0, DenySettings())
def no_network(*args, **kwargs):
    raise AssertionError("initialization performed a network dial")
socket.socket.connect = no_network
socket.create_connection = no_network
from cli import main, start_intent
from shared import cluster
home = Path(os.environ["AVA_HOME"])
checkout = home.parent / "checkout"
checkout.mkdir()
start_intent._checkout = lambda: checkout
cluster._port_free = lambda _port: True
calls = []
def configured():
    assert (home / "start-intent.json").is_file()
    assert (home / ".env").is_file()
    assert Path(os.environ["AVA_CLUSTER_REGISTRY"]).is_file()
def log():
    configured()
    calls.append("logging")
main._init_detached_cli_logging = log
def start(**kwargs):
    configured()
    calls.append("runtime")
    return 0
sys.modules["cli.commands.start"] = types.SimpleNamespace(cmd_start=start)
sys.modules["cli.commands._root_driver"] = types.SimpleNamespace(
    complete_boot_start=lambda: calls.append("boot-complete")
)
sys.modules["shared.start_serving"] = types.SimpleNamespace(
    clear_serving=lambda: calls.append("clear-serving")
)
assert main.main(["start", "--worktree"]) == 0
assert calls == ["logging", "runtime", "boot-complete"]
assert "shared.config" not in sys.modules
"""
    env = {key: value for key, value in os.environ.items() if not key.startswith("AVA_")}
    env.update(
        AVA_HOME=str(tmp_path / "home"),
        AVA_HOME_OVERRIDE="1",
        AVA_CLUSTER_REGISTRY=str(tmp_path / "clusters.json"),
        AVA_CLI_LOG_NAME="first-start",
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
    code = """
import importlib.abc
import sys
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'shared.config':
            raise AssertionError('forbidden early import: ' + fullname)
sys.meta_path.insert(0, Deny())
import cli.commands
assert not any(name.startswith('cli.commands.') for name in sys.modules)
assert not hasattr(cli.commands, '__getattr__')
assert not hasattr(cli.commands, '__all__')
if sys.argv[1] == 'parser':
    from cli.parsers import build_parser
    parser = build_parser()
    args = parser.parse_args(['cluster', 'update', '--prepared', '/unused/request'])
    assert args.prepared == '/unused/request'
elif sys.argv[1] == 'config':
    import cli.commands.config
assert 'shared.config' not in sys.modules
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

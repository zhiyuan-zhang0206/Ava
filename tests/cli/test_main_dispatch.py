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
from pathlib import Path

import pytest

from cli import main as _main

# Each top-level (and nested) ava sub-command maps to a _h_* handler in cli.main.
_HANDLERS: tuple[tuple[list[str], str], ...] = (
    (["stop"], "_h_stop"),
    (["restart"], "_h_restart"),
    (["status"], "_h_status"),
    (["pty", "freeze", "--holder", "operator", "--reason", "cleanup"], "_h_pty_freeze"),
    (["pty", "status"], "_h_pty_status"),
    (["pty", "resume", "generation"], "_h_pty_resume"),
    (["cluster", "update"], "_h_cluster_update"),
    (["converge"], "_h_converge"),
    (["firewall", "status"], "_h_firewall_status"),
    (["firewall", "sync"], "_h_firewall_sync"),
    (["cluster", "status"], "_h_cluster_status"),
    (["cluster", "restart"], "_h_cluster_restart"),
    (["cluster", "ensure-db-role"], "_h_cluster_ensure_db_role"),
    # The pre-#217 name stays as an alias — both spellings route to the same
    # handler (issue #217: the verb provisions the ava_runner POSTGRES role,
    # not a machine capability).
    (["cluster", "ensure-runner-role"], "_h_cluster_ensure_db_role"),
    (["plugins", "update"], "_h_plugins_update"),
    (["agents", "ls"], "_h_agents_ls"),
    (["agents", "cancel", "1"], "_h_agents_cancel"),
    (["agents", "restart", "1"], "_h_agents_restart"),
    (["agents", "terminate", "1"], "_h_agents_terminate"),
    (["agents", "kill", "1"], "_h_agents_kill"),
    (["agents", "resurrect", "1"], "_h_agents_resurrect"),
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
    import cli.commands as _commands

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(_commands, "cmd_status", lambda **kwargs: calls.append(kwargs) or 0)  # pyright: ignore[reportUnknownArgumentType]

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


def test_cluster_update_parser_rejects_dry_run_with_restart_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Prepare checks have no meaningful restart-only target, so reject the pair early."""
    with pytest.raises(SystemExit) as exited:
        _main._build_parser().parse_args(["cluster", "update", "--dry-run", "--restart-only"])

    assert exited.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


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
    import cli.preflight as _preflight

    monkeypatch.setattr(_preflight, "require_installed_home", lambda: None)
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
    """`ava stop` / `status` / `cluster watchdog-probe` set AVA_CONFIG_FETCH=skip
    before dispatch (settings-lite: they must work while the gateway is down);
    `ava start` does not (its Settings build fetches, fail-fast)."""
    import os as _os

    import cli.preflight as _preflight

    # Blanking os.environ drops AVA_HOME, which makes this checkout read as
    # unanchored — so `stop` would hit the anchored-home gate. Neutralize it the
    # same way `start`'s installed-home gate is neutralized below: this test
    # asserts env pinning, not gate behaviour.
    monkeypatch.setattr(_preflight, "require_anchored_home", lambda _verb: None)  # pyright: ignore[reportUnknownArgumentType]
    for verb in ("stop", "status", "cluster", "agents", "config", "logs"):
        env = {"PATH": "/usr/bin"}
        monkeypatch.setattr(_os, "environ", env)
        monkeypatch.setattr(_main, "_build_parser", lambda v=verb: _noop_parser(v))
        assert _main.main([verb]) == 0
        assert env.get("AVA_CONFIG_FETCH") == "skip", f"{verb} must be settings-lite"

    # start is NOT lite: the fetch happens at its Settings build
    env = {"PATH": "/usr/bin"}
    monkeypatch.setattr(_os, "environ", env)
    import cli.preflight as _preflight

    monkeypatch.setattr(_preflight, "require_installed_home", lambda: None)
    monkeypatch.setattr(_main, "_build_parser", lambda: _noop_parser("start"))
    assert _main.main(["start"]) == 0
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
        ["cluster", "update"],
        ["cluster", "restart"],
        ["cluster", "rollback"],
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
    assert "install.sh --worktree" in err


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

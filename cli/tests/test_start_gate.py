"""`ava start` admits only a home `ava init` initialized, and takes no identity input."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import cli.main as cli_main
from base import cluster
from cli import start_identity, start_intent, unit_join
from cli.database import OperatorDatabaseFactory, operator_event_pipeline
from cli.parsers import build_parser
from tests.path_scoped.cli_tests import operator_database as operator_database

_GATEWAY = {"gateway", "agent-runner"}


def _every_port_free(_port: int) -> bool:
    return True


def _no_logging(_argv: list[str], *, producer: Callable[[], Any]) -> None:
    return None


@pytest.fixture(autouse=True)
def _isolate_environment() -> Iterator[None]:
    """A real start prunes derived keys from the process; that ends with the test."""
    with patch.dict(os.environ):
        yield


@pytest.fixture
def tools(tmp_path: Path) -> str:
    return str(tmp_path / "tools")


@pytest.fixture
def start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The runtime half of `ava start` replaced by a recorder: the gate is under test."""
    calls: list[dict[str, Any]] = []

    def runtime(**kwargs: Any) -> int:
        calls.append(kwargs)
        return 0

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(start_intent, "_checkout", lambda: checkout)
    monkeypatch.setattr(cluster, "port_free", _every_port_free)
    monkeypatch.setattr(cli_main, "_init_cli_logging", _no_logging)
    monkeypatch.setattr("cli.commands.lifecycle.start.cmd_start", runtime)
    monkeypatch.setattr("cli.commands.lifecycle.root_driver.complete_boot_start", lambda: None)
    return calls


def _home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: str,
    roles: set[str],
    **extra: str,
) -> Path:
    """A home `ava init` would have left: intent and `.env` published."""
    home = tmp_path / "home"
    values = {"AVA_MACHINE_NAME": "gate", "AVA_SERVICE_PATH": tools, **extra}
    start_identity.prepare_identity(
        start_identity.IdentityInput(home, tmp_path / "checkout", frozenset(roles), values)
    )
    monkeypatch.setenv("AVA_HOME", str(home))
    return home


def _start(*flags: str, operator_database: OperatorDatabaseFactory) -> int:
    producer = operator_event_pipeline(operator_database)
    return start_intent.run_start(
        build_parser().parse_args(["start", *flags]),
        retained_children=[],
        database_factory=operator_database,
        producer=producer,
    )


def _bytes(home: Path) -> list[tuple[bytes, int]]:
    return [
        (p.read_bytes(), p.stat().st_mtime_ns)
        for p in (home / ".env", home / start_identity.INTENT_NAME)
    ]


def test_a_missing_home_is_refused_and_nothing_is_made(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start: list[dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
) -> None:
    home = tmp_path / "never-initialized"
    monkeypatch.setenv("AVA_HOME", str(home))

    assert _start(operator_database=operator_database) == 1

    assert "not initialized: run `ava init` first" in capsys.readouterr().err
    assert not home.exists() and start == []


def test_resources_without_an_intent_are_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start: list[dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
) -> None:
    home = tmp_path / "home"
    (home / "pg").mkdir(parents=True)
    monkeypatch.setenv("AVA_HOME", str(home))

    assert _start(operator_database=operator_database) == 1

    assert "no initialization authority" in capsys.readouterr().err
    assert start == []


def test_a_claim_in_progress_sends_the_operator_back_to_init(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: str,
    start: list[dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
) -> None:
    home = _home(tmp_path, monkeypatch, tools, _GATEWAY)
    path = home / start_identity.INTENT_NAME
    path.write_text(path.read_text().replace('"phase": "configured"', '"phase": "claiming"'))

    assert _start(operator_database=operator_database) == 1

    err = capsys.readouterr().err
    assert "partly initialized" in err and "Re-run `ava init`" in err
    assert start == []


def test_a_detached_home_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: str,
    start: list[dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
) -> None:
    home = _home(tmp_path, monkeypatch, tools, _GATEWAY)
    (home / "destroy-intent.json").write_text('{"version":1,"state":"detached"}\n')

    assert _start(operator_database=operator_database) == 1

    assert "destroyed or detached" in capsys.readouterr().err
    assert start == []


@pytest.mark.parametrize("phase", ["configured", "provisioned", "ready"])
def test_an_initialized_home_starts_with_its_service_selection_and_is_left_as_it_was(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: str,
    start: list[dict[str, Any]],
    phase: str,
    operator_database: OperatorDatabaseFactory,
) -> None:
    home = _home(tmp_path, monkeypatch, tools, _GATEWAY)
    start_identity.mark_phase(home, phase)
    before = _bytes(home)

    assert _start("--only-service", "gateway", operator_database=operator_database) == 0

    assert [c["only_services"] for c in start] == [("gateway",)]
    assert _bytes(home) == before


def test_start_never_joins_a_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: str,
    start: list[dict[str, Any]],
    operator_database: OperatorDatabaseFactory,
) -> None:
    """The join (bundle, bootstrap fetch) belongs to init and install-unit: a started
    runner fetches its configuration through Settings and probes its gateway itself."""

    def refused(*_a: object, **_k: object) -> None:
        raise AssertionError("start must not join the gateway")

    monkeypatch.setattr(unit_join, "join_gateway", refused)
    _home(
        tmp_path,
        monkeypatch,
        tools,
        {"agent-runner"},
        AVA_GATEWAY_URL="http://10.0.0.7:8000",
    )

    assert _start(operator_database=operator_database) == 0
    assert len(start) == 1


def _rewrite_env(home: Path, edit: Callable[[list[str]], list[str]]) -> None:
    env = home / ".env"
    env.write_text("\n".join(edit(env.read_text().splitlines())) + "\n")


@pytest.mark.parametrize(
    ("roles", "drop", "message"),
    [
        (_GATEWAY, "AVA_DB_URL=", "incomplete; explicit reattachment is required"),
        (_GATEWAY, "AVA_REDIS_URL=", "incomplete; explicit reattachment is required"),
        ({"agent-runner"}, "AVA_GATEWAY_URL=", "no gateway identity"),
        (_GATEWAY, "AVA_SERVICE_PATH=", "explicit AVA_SERVICE_PATH declaration"),
    ],
)
def test_an_incomplete_home_is_refused_before_any_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: str,
    start: list[dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
    roles: set[str],
    drop: str,
    message: str,
    operator_database: OperatorDatabaseFactory,
) -> None:
    extra = {} if "gateway" in roles else {"AVA_GATEWAY_URL": "http://10.0.0.7:8000"}
    home = _home(tmp_path, monkeypatch, tools, roles, **extra)
    _rewrite_env(home, lambda lines: [ln for ln in lines if not ln.startswith(drop)])

    assert _start(operator_database=operator_database) == 1

    assert message in capsys.readouterr().err
    assert start == []


def test_a_remote_unit_that_records_the_human_secret_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: str,
    start: list[dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
) -> None:
    home = _home(
        tmp_path, monkeypatch, tools, {"agent-runner"}, AVA_GATEWAY_URL="http://10.0.0.7:8000"
    )
    _rewrite_env(home, lambda lines: [*lines, "AVA_CLUSTER_SECRET=leaked"])

    assert _start(operator_database=operator_database) == 1

    assert "records the human cluster secret" in capsys.readouterr().err
    assert start == []


@pytest.mark.parametrize(
    "env",
    [
        pytest.param("AVA_GATEWAY_URL=http://10.0.0.7:8000\n", id="runner"),
        pytest.param("AVA_DB_URL=postgresql://db.invalid/ava\n", id="gateway"),
    ],
)
def test_a_home_with_an_identity_but_no_intent_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: str,
    start: list[dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
    env: str,
    operator_database: OperatorDatabaseFactory,
) -> None:
    """A home with no `start-intent.json` has no admission, whatever its `.env` and
    capability flags declare: `ava start` takes an identity only from `ava init`."""
    home = tmp_path / "predates-the-intent"
    home.mkdir(mode=0o700)
    (home / ".env").write_text(
        f"{env}AVA_SERVICE_PATH={tools}\nAVA_MACHINE_SERVE_AGENT_RUNNER=true\n"
    )
    monkeypatch.setenv("AVA_HOME", str(home))

    assert _start(operator_database=operator_database) == 1

    assert "no recorded identity" in capsys.readouterr().err
    assert start == []
    assert not (home / start_identity.INTENT_NAME).exists()  # a start never writes an identity

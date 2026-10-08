"""`ava init` records a home's identity once, starts nothing, and refuses a second run."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from dotenv import dotenv_values

from base import cluster
from cli import init_intent, start_identity, start_intent
from cli.parsers import build_parser


def _every_port_free(_port: int) -> bool:
    return True


def _power_loss(*_args: object, **_kwargs: object) -> None:
    raise OSError("power loss")


@pytest.fixture(autouse=True)
def _isolate_environment() -> Iterator[None]:
    """`init` pins `SSL_CERT_FILE` into the process for a joining runner; that ends with the test."""
    with patch.dict(os.environ):
        yield


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(start_intent, "_checkout", lambda: checkout)
    monkeypatch.setattr(cluster, "port_free", _every_port_free)
    monkeypatch.setenv("AVA_HOME", str(home))
    os.environ["AVA_SERVICE_PATH"] = str(tmp_path / "tools")  # restored by the autouse fixture
    return home


def _init(*flags: str) -> int:
    return init_intent.run_init(build_parser().parse_args(["init", *flags]))


_SINGLE_BOX = ("--serve-gateway", "--serve-agent-runner", "--machine-name", "probe")


def _state(home: Path) -> list[tuple[bytes, int]]:
    paths = [home / ".env", home / start_identity.INTENT_NAME]
    return [(p.read_bytes(), p.stat().st_mtime_ns) for p in paths]


def test_init_publishes_the_identity_starts_nothing_and_names_the_next_step(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _init(*_SINGLE_BOX) == 0

    intent = start_identity.read_intent(home)
    assert intent is not None and intent["phase"] == "configured"
    assert intent["roles"] == ["agent-runner", "gateway"]
    env = dotenv_values(home / ".env")
    assert env["AVA_MACHINE_NAME"] == "probe" and env["AVA_DB_URL"]
    # No native resource exists: the first `ava start` provisions the data plane.
    assert not any((home / name).exists() for name in ("pg", "redis", "pgbouncer", "run"))
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("next: run `") and last.endswith(" start`")


@pytest.mark.parametrize("phase", ["configured", "provisioned", "ready"])
def test_init_refuses_an_initialized_home_and_changes_nothing(
    home: Path, capsys: pytest.CaptureFixture[str], phase: str
) -> None:
    assert _init(*_SINGLE_BOX) == 0
    start_identity.mark_phase(home, phase)
    before = _state(home)
    capsys.readouterr()

    assert _init(*_SINGLE_BOX) == 1

    err = capsys.readouterr().err
    assert f"already initialized (phase {phase})" in err
    assert "`ava start`" in err and "`ava cluster destroy`" in err
    assert _state(home) == before


def test_an_interrupted_claim_resumes_from_its_recorded_payload_without_flags(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write = start_identity.upsert_env

    monkeypatch.setattr(start_identity, "upsert_env", _power_loss)
    assert _init(*_SINGLE_BOX) == 1
    pending = start_identity.read_intent(home)
    assert pending is not None and pending["phase"] == "claiming"
    assert not (home / ".env").exists()

    monkeypatch.setattr(start_identity, "upsert_env", write)
    assert _init() == 0
    assert cluster.get_record(home) == cluster.ClusterRecord(**pending["record"])
    assert dict(dotenv_values(home / ".env")) == pending["env"]
    done = start_identity.read_intent(home)
    assert done is not None and done["phase"] == "configured" and done["env"] == {}
    assert "initialized" in capsys.readouterr().out


def test_an_interrupted_claim_takes_no_flags(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(start_identity, "upsert_env", _power_loss)
    assert _init(*_SINGLE_BOX) == 1
    capsys.readouterr()

    assert _init("--machine-name", "other", "--serve-gateway") == 1

    err = capsys.readouterr().err
    assert "without flags" in err and "--machine-name" in err and "--serve-gateway" in err
    pending = start_identity.read_intent(home)
    assert pending is not None and pending["phase"] == "claiming"
    assert not (home / ".env").exists()


def test_init_refuses_a_detached_home(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home.mkdir(mode=0o700)
    (home / "destroy-intent.json").write_text('{"version":1,"state":"detached"}\n')

    assert _init(*_SINGLE_BOX) == 1

    assert "destroyed or detached" in capsys.readouterr().err
    assert not (home / start_identity.INTENT_NAME).exists()


def test_init_refuses_configuration_without_an_intent(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A home that has an identity in its `.env` but no intent has no initialization
    authority: init does not adopt it, and `ava start` refuses it the same way."""
    home.mkdir(mode=0o700)
    (home / ".env").write_text("AVA_GATEWAY_URL=http://10.0.0.7:8000\n")

    assert _init("--serve-agent-runner", "--no-serve-gateway", "--machine-name", "r") == 1

    assert "no recorded identity" in capsys.readouterr().err
    assert not (home / start_identity.INTENT_NAME).exists()


def test_init_without_inputs_names_this_checkouts_cli(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The host's `ava` is linked only by a first start, so the examples name the
    checkout's own `.venv/bin/ava`."""
    assert _init() == 1

    err = capsys.readouterr().err
    assert "no capability declared" in err
    assert f"{tmp_path / 'checkout'}/.venv/bin/ava init --machine-name <name>" in err
    assert not home.exists() or not (home / start_identity.INTENT_NAME).exists()


def test_init_requires_a_machine_name(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _init("--serve-gateway", "--serve-agent-runner") == 1

    assert "init requires --machine-name" in capsys.readouterr().err
    assert not (home / start_identity.INTENT_NAME).exists()


_INIT_ONLY_FLAGS = [
    ["--machine-name", "x"],
    ["--serve-gateway"],
    ["--no-serve-gateway"],
    ["--serve-agent-runner"],
    ["--no-serve-agent-runner"],
    ["--serve-observability-station"],
    ["--machine-description", "x"],
    ["--memory-remote", "x"],
    ["--gateway-url", "http://x"],
    ["--config-file", "x"],
    ["--machine-host", "x"],
    ["--ssl-cert-file", "x"],
    ["--db-capability", "x"],
]
_START_ONLY_FLAGS = [
    ["--only-service", "gateway"],
    ["--disable-service", "frontend"],
    ["--all-services"],
    ["--persist-services"],
]


@pytest.mark.parametrize("flags", _INIT_ONLY_FLAGS, ids=lambda f: f[0])
def test_start_takes_no_identity_flag(flags: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["start", *flags])
    assert error.value.code == 2
    build_parser().parse_args(["init", *flags])  # init owns it


@pytest.mark.parametrize("flags", _START_ONLY_FLAGS, ids=lambda f: f[0])
def test_init_takes_no_service_selection(flags: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["init", *flags])
    assert error.value.code == 2
    build_parser().parse_args(["start", *flags])  # start owns it

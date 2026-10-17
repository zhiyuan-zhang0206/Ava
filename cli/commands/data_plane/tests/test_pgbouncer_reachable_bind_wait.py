"""Reachable-bind wait + degraded-bind alert in the pgbouncer bring-up (task #1288).

pgbouncer treats a failed bind on one `listen_addr` entry as a WARNING and keeps
running on the rest. When `meiva start` races the private network at boot, the
pooler comes up loopback-only, a loopback-only readiness probe reads it as
healthy, and the pooled MEIVA_DB_URL public path stays silently dead for every
enrolled agent-runner (2026-08-16: two days).

What is asserted here — the decision logic around `ensure_pgbouncer`:

- the bring-up waits (bounded) for the reachable bind address ONLY on the paths
  that actually (re)start a pooler (`wait_for_reachable_bind` returns at once on
  a host whose reachable address is loopback);
- a fresh start that comes up loopback-only aborts with an explicit error, never
  a "✓ pgbouncer started";
- a running pooler is reloaded only when its public listener verifies; a degraded
  running pooler is RESTARTED (a SIGHUP reload cannot re-bind a listen_addr that
  failed at startup), and a terminate that did not take is reported.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from base.cluster.dataplane import pooler as base_pooler
from base.cluster.machine import MachineIdentity
from cli.commands.data_plane import pgbouncer as _pb
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime

_SECRET = "s3cr3t"  # noqa: S105 — test fixture, not a real credential
_ADMIN = "pooler-admin-fixture"
_USERLIST = b'"meiva_pooler_admin" "SCRAM-SHA-256$4096:c2FsdA==$a:b"\n'


class _ReloadProcess:
    def __init__(self, pid: int, signals: list[int]) -> None:
        self.pid = pid
        self.signals = signals

    def send_signal(self, _signal: int) -> None:
        self.signals.append(self.pid)


@pytest.fixture()
def _noop_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the real $MEIVA_HOME/pgbouncer dir from a unit test."""
    monkeypatch.setattr(
        _pb.port_preflight, "require_listener", Mock(return_value=frozenset({4242}))
    )
    monkeypatch.setattr(_pb.OwnedPooler, "_stop_requested", Mock(return_value=False))
    # Unchanged files: the reload-vs-restart decision is the bind posture alone.
    monkeypatch.setattr(_pb, "write_config", lambda **_kw: False)  # pyright: ignore[reportUnknownArgumentType]
    # ensure_pgbouncer's binary-exists guard must pass on every runner: the fake
    # `pgbouncer_bin` path only exists on macOS (brew), and CI is Linux without
    # pgbouncer installed. `shutil.which` answering non-None satisfies the guard.
    monkeypatch.setattr(
        _pb.shutil,
        "which",
        lambda _bin: "/usr/bin/pgbouncer",  # pyright: ignore[reportUnknownArgumentType]
    )
    # pgbouncer_bin shells out (`brew --prefix pgbouncer` on macOS); stub it so the
    # fake start path never touches subprocess.
    monkeypatch.setattr(base_pooler, "pgbouncer_bin", lambda: "/opt/homebrew/bin/pgbouncer")


def _fake_start(monkeypatch: pytest.MonkeyPatch, *, rc: int = 0) -> list[list[str]]:
    """Stub the pgbouncer subprocess launch; returns the argv it was called with."""
    calls: list[list[str]] = []

    def _run(cmd: list[str], **_: object) -> object:
        calls.append(cmd)
        return type("R", (), {"returncode": rc, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(_pb.subprocess, "run", _run)
    return calls


# ── the wait: interface not ready ────────────────────────────────────────────


def test_fresh_start_waits_for_reachable_bind_and_fails_fast_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
    _noop_write: None,
    capsys: pytest.CaptureFixture[str],
    *,
    command_machine: MachineIdentity,
) -> None:
    """The reachable address is not up within the bound: a fresh start must NOT
    birth a pooler that would silently degrade to loopback-only. Fail fast with an
    explicit message; the boot retry re-runs `meiva start` once the network is up."""
    monkeypatch.setattr(_pb.ownership, "pooler", Mock(return_value=None))

    def fake_wait_for_reachable_bind(*, machine: MachineIdentity) -> bool:
        assert isinstance(machine, MachineIdentity)
        return False

    monkeypatch.setattr(_pb, "wait_for_reachable_bind", fake_wait_for_reachable_bind)  # pyright: ignore[reportUnknownArgumentType]
    command_machine.set(host="10.0.0.5")
    calls = _fake_start(monkeypatch)

    rc = _pb.ensure_pgbouncer(
        pg_port=5433,
        listen_port=6433,
        db_name="meiva_main",
        userlist=_USERLIST,
        admin_password=_ADMIN,
        machine=command_machine,
    )

    assert rc == 1
    assert calls == [], "pgbouncer must not be launched when the bind address is absent"
    err = capsys.readouterr().err
    assert "not assigned to any local interface" in err
    assert "private network" in err
    assert "silently degrade" in err


# ── the alert: bind failure path must be loud ────────────────────────────────


def test_fresh_start_degraded_to_loopback_only_is_a_loud_failure(
    monkeypatch: pytest.MonkeyPatch,
    _noop_write: None,
    capsys: pytest.CaptureFixture[str],
    *,
    command_machine: MachineIdentity,
) -> None:
    """The pooler came up (admin console answers on 127.0.0.1) but the reachable
    listener is missing — the exact silent degradation. The bring-up must abort
    with an explicit error, not print "✓ pgbouncer started"."""

    def fake_wait_for_reachable_bind(*, machine: MachineIdentity) -> bool:
        assert isinstance(machine, MachineIdentity)
        return True

    monkeypatch.setattr(_pb, "wait_for_reachable_bind", fake_wait_for_reachable_bind)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_pb.ownership, "pooler", Mock(return_value=None))
    monkeypatch.setattr(base_pooler, "admin_reachable", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        base_pooler,
        "pgbouncer_public_listener_reachable",
        lambda *_a, **_kw: False,  # pyright: ignore[reportUnknownArgumentType]
    )
    command_machine.set(host="10.0.0.5")
    _fake_start(monkeypatch)

    rc = _pb.ensure_pgbouncer(
        pg_port=5433,
        listen_port=6433,
        db_name="meiva_main",
        userlist=_USERLIST,
        admin_password=_ADMIN,
        machine=command_machine,
    )

    assert rc == 1
    out = capsys.readouterr()
    assert "✓ pgbouncer started" not in out.out
    assert "NOT listening on the reachable address" in out.err
    assert "10.0.0.5" in out.err
    assert "degraded to loopback-only" in out.err


def test_fresh_start_with_public_listener_is_success(
    monkeypatch: pytest.MonkeyPatch,
    _noop_write: None,
    capsys: pytest.CaptureFixture[str],
    *,
    command_machine: MachineIdentity,
) -> None:
    """Healthy double bind: loopback + reachable both answer → success as before."""

    def fake_wait_for_reachable_bind(*, machine: MachineIdentity) -> bool:
        assert isinstance(machine, MachineIdentity)
        return True

    monkeypatch.setattr(_pb, "wait_for_reachable_bind", fake_wait_for_reachable_bind)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_pb.ownership, "pooler", Mock(return_value=None))
    monkeypatch.setattr(base_pooler, "admin_reachable", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(base_pooler, "pgbouncer_public_listener_reachable", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    _fake_start(monkeypatch)

    rc = _pb.ensure_pgbouncer(
        pg_port=5433,
        listen_port=6433,
        db_name="meiva_main",
        userlist=_USERLIST,
        admin_password=_ADMIN,
        machine=command_machine,
    )

    assert rc == 0
    assert "✓ pgbouncer started" in capsys.readouterr().out


# Delivered to an operator's `meiva start` by the boot pass; a daemon it spawns
# must not keep them in its environment until it restarts.
_AUTHORITY_ENV = {
    "MEIVA_DB_URL": "postgresql://meiva_g0_gateway:gen-password@127.0.0.1:6433/meiva",
    "MEIVA_DB_GENERATION": "0",
    "MEIVA_API_TOKEN": "gateway-api-token-" + "t" * 32,
    "MEIVA_CLUSTER_SECRET": "human-" + "h" * 40,
    "MEIVA_REDIS_ADMIN_PASSWORD": "redis-admin-" + "r" * 20,
}


def test_the_pooler_daemon_inherits_no_meiva_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _noop_write: None,
    *,
    command_machine: MachineIdentity,
) -> None:
    # The operator's home is a real directory the code may create: `meiva_home()` makes
    # its home, so a made-up `/Users/...` path would be created (or refused) on the host.
    authority = {**_AUTHORITY_ENV, "MEIVA_HOME": str(tmp_path / ".meiva-operator")}
    for key, value in {**authority, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}.items():
        monkeypatch.setenv(key, value)

    def fake_wait_for_reachable_bind(*, machine: MachineIdentity) -> bool:
        assert isinstance(machine, MachineIdentity)
        return True

    monkeypatch.setattr(_pb, "wait_for_reachable_bind", fake_wait_for_reachable_bind)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_pb.ownership, "pooler", Mock(return_value=None))
    monkeypatch.setattr(base_pooler, "admin_reachable", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(base_pooler, "pgbouncer_public_listener_reachable", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    envs: list[object] = []

    def _run(cmd: list[str], **kwargs: object) -> object:
        assert cmd[1] == "-d"
        envs.append(kwargs.get("env"))
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(_pb.subprocess, "run", _run)
    assert (
        _pb.ensure_pgbouncer(
            pg_port=5433,
            listen_port=6433,
            db_name="meiva_main",
            userlist=_USERLIST,
            admin_password=_ADMIN,
            machine=command_machine,
        )
        == 0
    )
    [captured] = envs
    assert isinstance(captured, dict)
    env = cast("dict[str, str]", captured)
    assert env["PATH"] == "/usr/bin:/bin" and env["LANG"] == "C.UTF-8"
    assert not [key for key in env if key.startswith("MEIVA_")]
    assert not set(authority.values()) & set(env.values())


# ── the reload-vs-restart decision ───────────────────────────────────────────


def test_running_pooler_is_reloaded_when_public_listener_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
    _noop_write: None,
    *,
    command_machine: MachineIdentity,
) -> None:
    """A running, fully-bound pooler keeps the cheap reload path — never bounce
    live connections for nothing, and never wait on the reachable address."""
    killed: list[int] = []

    def record_stop(custodian: _pb.OwnedPooler, **_kwargs: object) -> bool:
        killed.append(custodian.identity.pid)
        return True

    sighups: list[int] = []

    monkeypatch.setattr(
        _pb.ownership, "pooler", Mock(return_value=SimpleNamespace(pid=4242, live=lambda: True))
    )
    monkeypatch.setattr(
        _pb.OwnedPooler,
        "stop",
        record_stop,
    )
    monkeypatch.setattr(
        _pb,
        "psutil",
        SimpleNamespace(Process=Mock(return_value=_ReloadProcess(4242, sighups))),
    )
    monkeypatch.setattr(base_pooler, "pgbouncer_public_listener_reachable", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(base_pooler, "admin_reachable", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]

    def fake_wait_for_reachable_bind(*, machine: MachineIdentity) -> bool:
        assert isinstance(machine, MachineIdentity)
        return pytest.fail("a healthy running pooler must never wait")

    monkeypatch.setattr(
        _pb,
        "wait_for_reachable_bind",
        fake_wait_for_reachable_bind,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _pb, "subprocess", type("_SP", (), {"run": staticmethod(lambda *_a, **_kw: None)})()
    )
    rc = _pb.ensure_pgbouncer(
        pg_port=5433,
        listen_port=6433,
        db_name="meiva_main",
        userlist=_USERLIST,
        admin_password=_ADMIN,
        machine=command_machine,
    )

    assert rc == 0
    assert killed == [], "a healthy pooler must be reloaded, not restarted"
    assert sighups == [4242], "the healthy pooler gets the SIGHUP reload"


def test_running_degraded_pooler_is_restarted_not_reloaded(
    monkeypatch: pytest.MonkeyPatch,
    _noop_write: None,
    capsys: pytest.CaptureFixture[str],
    *,
    command_machine: MachineIdentity,
) -> None:
    """A running pooler missing its public listener is degraded (born in a bind
    race). Reload cannot fix it — pgbouncer never retries a listen_addr that failed
    at startup — so the bring-up must tear it down and start fresh, loudly."""
    killed: list[int] = []

    def record_stop(custodian: _pb.OwnedPooler, **_kwargs: object) -> bool:
        killed.append(custodian.identity.pid)
        return True

    sighups: list[int] = []
    public_answers = iter([False, True])  # degraded before restart, healthy after

    monkeypatch.setattr(
        _pb.ownership, "pooler", Mock(return_value=SimpleNamespace(pid=4242, live=lambda: True))
    )
    monkeypatch.setattr(
        _pb.OwnedPooler,
        "stop",
        record_stop,
    )
    monkeypatch.setattr(
        _pb,
        "psutil",
        SimpleNamespace(Process=Mock(return_value=_ReloadProcess(4242, sighups))),
    )
    monkeypatch.setattr(
        base_pooler,
        "pgbouncer_public_listener_reachable",
        lambda *_a, **_kw: next(public_answers),  # pyright: ignore[reportUnknownArgumentType]
    )

    def fake_wait_for_reachable_bind(*, machine: MachineIdentity) -> bool:
        assert isinstance(machine, MachineIdentity)
        return True

    monkeypatch.setattr(_pb, "wait_for_reachable_bind", fake_wait_for_reachable_bind)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(base_pooler, "admin_reachable", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    command_machine.set(host="10.0.0.5")
    _fake_start(monkeypatch)

    rc = _pb.ensure_pgbouncer(
        pg_port=5433,
        listen_port=6433,
        db_name="meiva_main",
        userlist=_USERLIST,
        admin_password=_ADMIN,
        machine=command_machine,
    )

    assert rc == 0
    assert killed == [4242], "the degraded pooler must be restarted, not reloaded"
    assert sighups == [], "a degraded pooler must not get a pointless SIGHUP before the kill"
    err = capsys.readouterr().err
    assert "NOT listening on the reachable address" in err
    assert "restarting the pooler" in err


def test_running_degraded_pooler_surviving_terminate_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    _noop_write: None,
    capsys: pytest.CaptureFixture[str],
    *,
    command_machine: MachineIdentity,
) -> None:
    """P7: `OwnedPooler.stop` returning False means the degraded pooler survived
    the graceful stop — starting a second pooler on the same port would fail
    confusingly. The real cause must be said out loud."""
    monkeypatch.setattr(
        _pb.ownership, "pooler", Mock(return_value=SimpleNamespace(pid=4242, live=lambda: True))
    )
    monkeypatch.setattr(
        base_pooler,
        "pgbouncer_public_listener_reachable",
        lambda *_a, **_kw: False,  # pyright: ignore[reportUnknownArgumentType]
    )

    def fake_wait_for_reachable_bind(*, machine: MachineIdentity) -> bool:
        assert isinstance(machine, MachineIdentity)
        return True

    monkeypatch.setattr(_pb, "wait_for_reachable_bind", fake_wait_for_reachable_bind)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_pb.OwnedPooler, "stop", lambda *_a, **_kw: False)  # pyright: ignore[reportUnknownArgumentType]
    calls = _fake_start(monkeypatch)

    rc = _pb.ensure_pgbouncer(
        pg_port=5433,
        listen_port=6433,
        db_name="meiva_main",
        userlist=_USERLIST,
        admin_password=_ADMIN,
        machine=command_machine,
    )

    assert rc == 1
    assert calls == [], "no fresh start may follow a terminate that did not take"
    assert "could not stop the running pooler" in capsys.readouterr().err


def test_running_pooler_with_changed_userlist_is_restarted_never_reloaded(
    monkeypatch: pytest.MonkeyPatch,
    _noop_write: None,
    capsys: pytest.CaptureFixture[str],
    *,
    command_machine: MachineIdentity,
) -> None:
    """E11: a SIGHUP reload keeps a removed user that already authenticated and
    still admits its new sessions, so changed userlist bytes RESTART a healthy
    pooler; only a fresh process revokes."""
    killed: list[int] = []
    sighups: list[int] = []

    def record_stop(custodian: _pb.OwnedPooler, **_kwargs: object) -> bool:
        killed.append(custodian.identity.pid)
        return True

    monkeypatch.setattr(_pb, "write_config", lambda **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _pb.ownership, "pooler", Mock(return_value=SimpleNamespace(pid=4242, live=lambda: True))
    )
    monkeypatch.setattr(_pb.OwnedPooler, "stop", record_stop)
    monkeypatch.setattr(
        _pb, "psutil", SimpleNamespace(Process=Mock(return_value=_ReloadProcess(4242, sighups)))
    )
    monkeypatch.setattr(base_pooler, "pgbouncer_public_listener_reachable", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]

    def fake_wait_for_reachable_bind(*, machine: MachineIdentity) -> bool:
        assert isinstance(machine, MachineIdentity)
        return True

    monkeypatch.setattr(_pb, "wait_for_reachable_bind", fake_wait_for_reachable_bind)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(base_pooler, "admin_reachable", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    calls = _fake_start(monkeypatch)

    rc = _pb.ensure_pgbouncer(
        pg_port=5433,
        listen_port=6433,
        db_name="meiva_main",
        userlist=_USERLIST,
        admin_password=_ADMIN,
        machine=command_machine,
    )

    assert rc == 0
    assert killed == [4242] and sighups == [], "a changed userlist restarts, never reloads"
    assert len(calls) == 1, "a fresh pooler process serves the new userlist"
    assert "a reload never revokes" in capsys.readouterr().out

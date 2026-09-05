"""Reachable-bind wait + degraded-bind alert in the pgbouncer bring-up (task #1288).

pgbouncer treats a failed bind on one `listen_addr` entry as a WARNING and keeps
running on the rest. When `ava start` races the private network at boot, the
pooler comes up loopback-only, a loopback-only readiness probe reads it as
healthy, and the pooled AVA_DB_URL public path stays silently dead for every
enrolled agent-runner (2026-08-16: two days).

What is asserted here — the decision logic around `ensure_pgbouncer`:

- the bring-up waits (bounded) for the reachable bind address ONLY on the paths
  that actually (re)start a pooler, gated on the cluster secret (a no-secret
  cluster binds loopback only and never waits);
- a fresh start that comes up loopback-only aborts with an explicit error, never
  a "✓ pgbouncer started";
- a running pooler is reloaded only when its public listener verifies; a degraded
  running pooler is RESTARTED (a SIGHUP reload cannot re-bind a listen_addr that
  failed at startup), and a terminate that did not take is reported.
"""

from __future__ import annotations

import pytest

from cli.commands import _pgbouncer as _pb

_SECRET = "s3cr3t"  # noqa: S105 — test fixture, not a real credential


@pytest.fixture()
def _noop_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the real $AVA_HOME/pgbouncer dir from a unit test."""
    monkeypatch.setattr(_pb, "_write_config", lambda **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_pb, "_report_backend_verification", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
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
    monkeypatch.setattr(_pb, "pgbouncer_bin", lambda: "/opt/homebrew/bin/pgbouncer")


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
    monkeypatch: pytest.MonkeyPatch, _noop_write: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reachable address is not up within the bound: a fresh start must NOT
    birth a pooler that would silently degrade to loopback-only. Fail fast with an
    explicit message; the boot retry re-runs `ava start` once the network is up."""
    monkeypatch.setattr(_pb, "_running_pid", lambda: None)
    monkeypatch.setattr(_pb, "_wait_for_reachable_bind_gated", lambda _secret: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_pb, "reachable_host", lambda: "10.0.0.5")
    calls = _fake_start(monkeypatch)

    rc = _pb.ensure_pgbouncer(
        pg_port=5433,
        listen_port=6433,
        db_name="ava_main",
        role="ava_main",
        cluster_secret=_SECRET,
    )

    assert rc == 1
    assert calls == [], "pgbouncer must not be launched when the bind address is absent"
    err = capsys.readouterr().err
    assert "not assigned to any local interface" in err
    assert "private network" in err
    assert "silently degrade" in err


def test_wait_gate_skips_loopback_only_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2 gate: a no-secret cluster's pooler binds loopback only, so the wait must
    resolve True without ever probing the address — a stray AVA_MACHINE_HOST must
    not hold a warm start hostage."""
    probed: list[str] = []
    monkeypatch.setattr(_pb, "_bind_addrs", lambda _secret: ["127.0.0.1"])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _pb,
        "_wait_for_reachable_bind",
        lambda: probed.append("waited") or True,
    )

    assert _pb._wait_for_reachable_bind_gated("") is True
    assert probed == [], "loopback-only bind must never consult the reachable address"


def test_wait_gate_probes_when_reachable_is_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a secret the pooler binds loopback + the reachable address, so the
    wait consults `_wait_for_reachable_bind`."""
    monkeypatch.setattr(_pb, "_bind_addrs", lambda _secret: ["127.0.0.1", "10.0.0.5"])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_pb, "_wait_for_reachable_bind", lambda: True)
    assert _pb._wait_for_reachable_bind_gated(_SECRET) is True


# ── the alert: bind failure path must be loud ────────────────────────────────


def test_fresh_start_degraded_to_loopback_only_is_a_loud_failure(
    monkeypatch: pytest.MonkeyPatch, _noop_write: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pooler came up (admin console answers on 127.0.0.1) but the reachable
    listener is missing — the exact silent degradation. The bring-up must abort
    with an explicit error, not print "✓ pgbouncer started"."""
    monkeypatch.setattr(_pb, "_wait_for_reachable_bind_gated", lambda _secret: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_pb, "_running_pid", lambda: None)
    monkeypatch.setattr(_pb, "_admin_reachable", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_pb, "pgbouncer_public_listener_reachable", lambda *_a, **_kw: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_pb, "reachable_host", lambda: "10.0.0.5")
    _fake_start(monkeypatch)

    rc = _pb.ensure_pgbouncer(
        pg_port=5433,
        listen_port=6433,
        db_name="ava_main",
        role="ava_main",
        cluster_secret=_SECRET,
    )

    assert rc == 1
    out = capsys.readouterr()
    assert "✓ pgbouncer started" not in out.out
    assert "NOT listening on the reachable address" in out.err
    assert "10.0.0.5" in out.err
    assert "degraded to loopback-only" in out.err


def test_fresh_start_with_public_listener_is_success(
    monkeypatch: pytest.MonkeyPatch, _noop_write: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Healthy double bind: loopback + reachable both answer → success as before."""
    monkeypatch.setattr(_pb, "_wait_for_reachable_bind_gated", lambda _secret: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_pb, "_running_pid", lambda: None)
    monkeypatch.setattr(_pb, "_admin_reachable", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_pb, "pgbouncer_public_listener_reachable", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    _fake_start(monkeypatch)

    rc = _pb.ensure_pgbouncer(
        pg_port=5433,
        listen_port=6433,
        db_name="ava_main",
        role="ava_main",
        cluster_secret=_SECRET,
    )

    assert rc == 0
    assert "✓ pgbouncer started" in capsys.readouterr().out


# ── the reload-vs-restart decision ───────────────────────────────────────────


def test_running_pooler_is_reloaded_when_public_listener_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
    _noop_write: None,
) -> None:
    """A running, fully-bound pooler keeps the cheap reload path — never bounce
    live connections for nothing, and never wait on the reachable address."""
    killed: list[int] = []
    sighups: list[int] = []

    monkeypatch.setattr(_pb, "_running_pid", lambda: 4242)
    monkeypatch.setattr(
        _pb,
        "_terminate_verified",
        lambda pid, **_: killed.append(pid) or True,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _pb,
        "os",
        type("_OS", (), {"kill": staticmethod(lambda pid, _sig: sighups.append(pid))})(),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_pb, "pgbouncer_public_listener_reachable", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _pb,
        "_wait_for_reachable_bind_gated",
        lambda _secret: pytest.fail("a healthy running pooler must never wait"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _pb, "subprocess", type("_SP", (), {"run": staticmethod(lambda *_a, **_kw: None)})()
    )
    rc = _pb.ensure_pgbouncer(
        pg_port=5433,
        listen_port=6433,
        db_name="ava_main",
        role="ava_main",
        cluster_secret=_SECRET,
    )

    assert rc == 0
    assert killed == [], "a healthy pooler must be reloaded, not restarted"
    assert sighups == [4242], "the healthy pooler gets the SIGHUP reload"


def test_running_degraded_pooler_is_restarted_not_reloaded(
    monkeypatch: pytest.MonkeyPatch, _noop_write: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A running pooler missing its public listener is degraded (born in a bind
    race). Reload cannot fix it — pgbouncer never retries a listen_addr that failed
    at startup — so the bring-up must tear it down and start fresh, loudly."""
    killed: list[int] = []
    sighups: list[int] = []
    public_answers = iter([False, True])  # degraded before restart, healthy after

    monkeypatch.setattr(_pb, "_running_pid", lambda: 4242)
    monkeypatch.setattr(
        _pb,
        "_terminate_verified",
        lambda pid, **_: killed.append(pid) or True,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _pb,
        "os",
        type("_OS", (), {"kill": staticmethod(lambda pid, _sig: sighups.append(pid))})(),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _pb,
        "pgbouncer_public_listener_reachable",
        lambda *_a, **_kw: next(public_answers),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_pb, "_wait_for_reachable_bind_gated", lambda _secret: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_pb, "_admin_reachable", lambda *_a, **_kw: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_pb, "reachable_host", lambda: "10.0.0.5")
    _fake_start(monkeypatch)

    rc = _pb.ensure_pgbouncer(
        pg_port=5433,
        listen_port=6433,
        db_name="ava_main",
        role="ava_main",
        cluster_secret=_SECRET,
    )

    assert rc == 0
    assert killed == [4242], "the degraded pooler must be restarted, not reloaded"
    assert sighups == [], "a degraded pooler must not get a pointless SIGHUP before the kill"
    err = capsys.readouterr().err
    assert "NOT listening on the reachable address" in err
    assert "restarting the pooler" in err


def test_running_degraded_pooler_surviving_terminate_is_reported(
    monkeypatch: pytest.MonkeyPatch, _noop_write: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """P7: `_terminate_verified` returning False means the degraded pooler survived
    the force kill — starting a second pooler on the same port would fail
    confusingly. The real cause must be said out loud."""
    monkeypatch.setattr(_pb, "_running_pid", lambda: 4242)
    monkeypatch.setattr(_pb, "pgbouncer_public_listener_reachable", lambda *_a, **_kw: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_pb, "_wait_for_reachable_bind_gated", lambda _secret: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_pb, "_terminate_verified", lambda *_a, **_kw: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_pb, "os", type("_OS", (), {"kill": staticmethod(lambda *_a: None)})())
    calls = _fake_start(monkeypatch)

    rc = _pb.ensure_pgbouncer(
        pg_port=5433,
        listen_port=6433,
        db_name="ava_main",
        role="ava_main",
        cluster_secret=_SECRET,
    )

    assert rc == 1
    assert calls == [], "no fresh start may follow a terminate that did not take"
    assert "could not stop the degraded pooler" in capsys.readouterr().err

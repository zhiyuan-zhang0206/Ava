"""The pooler paths only ever signal a pid they have confirmed is their pooler.

The pidfile holds a bare pid, and a stale one — left behind whenever the pooler
is SIGKILLed rather than allowed to unlink it — eventually names a recycled
process. Untreated that wedges the cluster: `ensure_pgbouncer` reads the live
number as "already running", SIGHUPs a stranger, starts nothing, and never
rewrites the pidfile, so every later start repeats it. These pin the ownership
check on both signalling paths — the stop's SIGTERM and the start's reload
SIGHUP — and on the substring trap that would let one home claim another's.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import psutil
import pytest

from cli.commands import _pgbouncer as pgb
from shared.config import settings

_SECRET = "pgbouncerstopisolationtestsecret"  # noqa: S105 — test fixture, not a real credential


class _FakeProc:
    """A psutil.Process stand-in for a pid no test can really own.

    Carries both halves of the identity token, because which one names the config
    file is a platform difference — see the two `_pooler_proc` shapes below."""

    def __init__(self, name: str, cwd: Path, argv: list[str]) -> None:
        self._name = name
        self._cwd = cwd
        self._argv = argv

    def name(self) -> str:
        return self._name

    def cwd(self) -> str:
        return str(self._cwd)

    def cmdline(self) -> list[str]:
        return self._argv


def _pooler_proc(pgb_dir: Path, *, platform: str) -> _FakeProc:
    """A live pooler serving `pgb_dir`, in either platform's observed shape.

    Both were measured against a real pgbouncer:

      linux  apt 1.18 keeps the absolute ini path in argv and leaves cwd wherever
             the launcher stood (`/home/ava`, `/tmp` — never the config dir);
      macos  brew 1.25 chdir()s into the config dir and rewrites argv to a bare
             `pgbouncer.ini`.

    A check reading only one of the two identifies the pooler on one platform and
    silently fails on the other, which is what shipped and what broke Linux."""
    ini = pgb_dir / "pgbouncer.ini"
    if platform == "linux":
        return _FakeProc("pgbouncer", Path("/somewhere/else"), ["pgbouncer", "-d", str(ini)])
    return _FakeProc("pgbouncer", pgb_dir, ["/opt/pgbouncer/bin/pgbouncer", "-d", "pgbouncer.ini"])


class _CompletedOk:
    """A subprocess.CompletedProcess stand-in for the pgbouncer launch."""

    returncode = 0
    stderr = ""


def _fake_psutil_process(table: dict[int, _FakeProc]) -> Callable[[int], _FakeProc]:
    def factory(pid: int) -> _FakeProc:
        if pid not in table:
            raise psutil.NoSuchProcess(pid)
        return table[pid]

    return factory


def _write_pidfile(home: Path, pid: int) -> Path:
    pidfile = home / "pgbouncer" / "pgbouncer.pid"
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(pid))
    return pidfile


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch $AVA_HOME. Nothing here reaches a real cluster."""
    h = tmp_path / ".ava-scratch"
    h.mkdir()
    monkeypatch.setattr(settings.general, "ava_home", str(h))
    return h


@pytest.fixture()
def signalled(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every pid the stop path would signal. Replaces the SIGTERM/SIGKILL seam so
    a regression shows up as a recorded pid, not as a dead process."""
    calls: list[int] = []
    monkeypatch.setattr(pgb, "_terminate_verified", lambda pid, **_: calls.append(pid))  # pyright: ignore[reportUnknownArgumentType]
    return calls


def test_stop_does_not_signal_a_recycled_pid(home: Path, signalled: list[int]) -> None:
    """The pidfile names a live process that is not our pooler.

    This test's own interpreter is the stranger — a real, live, definitively
    not-pgbouncer pid, so nothing about the check is mocked away."""
    pidfile = _write_pidfile(home, os.getpid())

    pgb.stop_pgbouncer()

    assert signalled == []
    assert not pidfile.exists(), "the stale pidfile must be repaired, not re-read next time"


@pytest.mark.parametrize("platform", ["linux", "macos"])
def test_stop_signals_this_homes_own_pooler(
    platform: str, home: Path, signalled: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ownership check must not over-block: a real pooler still gets stopped,
    and its pidfile is left to the pooler's own clean-exit unlink."""
    pidfile = _write_pidfile(home, 4242)
    monkeypatch.setattr(
        psutil,
        "Process",
        _fake_psutil_process({4242: _pooler_proc(home / "pgbouncer", platform=platform)}),
    )

    pgb.stop_pgbouncer()

    assert signalled == [4242]
    assert pidfile.exists()


@pytest.mark.parametrize("platform", ["linux", "macos"])
def test_sibling_home_sharing_a_path_prefix_is_not_ours(
    platform: str, tmp_path: Path, signalled: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`~/.ava` is a prefix of `~/.ava-preview`: a substring match on the home path
    would hand the production stop a preview cluster's pooler (and vice versa)."""
    ours = tmp_path / ".ava"
    sibling = tmp_path / ".ava-preview"
    (sibling / "pgbouncer").mkdir(parents=True)
    monkeypatch.setattr(settings.general, "ava_home", str(ours))
    pidfile = _write_pidfile(ours, 4242)
    monkeypatch.setattr(
        psutil,
        "Process",
        _fake_psutil_process({4242: _pooler_proc(sibling / "pgbouncer", platform=platform)}),
    )

    pgb.stop_pgbouncer()

    assert signalled == []
    assert not pidfile.exists()


def test_start_does_not_reload_a_recycled_pid(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`ava start` reloads an already-running pooler with SIGHUP, which for most
    processes that are not pgbouncer is a kill — and start runs far more often
    than stop. A recycled pid must take the launch branch instead."""
    _write_pidfile(home, os.getpid())
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")  # loopback-only render
    monkeypatch.setattr(pgb, "pgbouncer_bin", lambda: str(Path(__file__)))  # exists; never run
    monkeypatch.setattr(pgb, "_admin_reachable", lambda *_a, **_k: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(pgb, "_report_backend_verification", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]

    signals: list[tuple[int, int]] = []

    def record_kill(pid: int, sig: int) -> None:
        if sig != 0:  # signal 0 is the liveness probe behind shared.proc.process_alive
            signals.append((pid, sig))

    monkeypatch.setattr(pgb.os, "kill", record_kill)

    launched: list[list[str]] = []

    def record_run(cmd: list[str], **_: object) -> _CompletedOk:
        launched.append(cmd)
        return _CompletedOk()

    monkeypatch.setattr(pgb.subprocess, "run", record_run)

    rc = pgb.ensure_pgbouncer(
        pg_port=15433,
        listen_port=16433,
        db_name="ava_scratch",
        role="ava_scratch",
        cluster_secret=_SECRET,
    )

    assert rc == 0
    assert signals == [], "no signal may reach a pid this home does not own"
    assert launched, "with the stale pidfile discarded, start must launch its own pooler"

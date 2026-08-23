"""`ava stop`'s orphan-listener sweep (Task #965) — the port-level closure.

The stop legs tear down what they can NAME (the service sessions, the native session
registry, the data plane, the registered extras); a service that escaped its
session is invisible to all of them and keeps the cluster port against the
next start — the new process dies on 'address already in use' while the old
one keeps serving (the 2026-08-07 pgbouncer / gate / gateway /
events-maintenance incident class). The sweep closes the gap: every port this
unit expects to own is scanned, and only a listener positively attributable to
the target cluster home is killed verified. These tests pin the sweep's
behavior: what it kills, what it never touches, and what it skips.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from cli.commands import _orphan_reap as _or
from cli.commands import stop as _stop


@pytest.fixture
def sweep_seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[int]]:
    """Point the sweep's source modules at fakes: the port map, the listener
    scan, the ownership predicate, and the verified-kill. The sweep imports
    them lazily from their source modules, so patching there is the seam."""
    state: dict[str, list[int]] = {"killed": [], "ours": []}

    monkeypatch.setattr(
        "shared.port_preflight.unit_port_map",
        lambda _home: {"gateway": 8000, "ops": 8106, "browser": 9222},  # pyright: ignore[reportUnknownArgumentType]
    )

    def _listeners(port: int) -> list[int]:
        return {8000: [101], 8106: [202], 9222: [303]}[port]

    monkeypatch.setattr("shared.port_preflight.listeners_on", _listeners)

    def _mentions(pid: int, markers: tuple[str, ...]) -> bool:
        return pid in state["ours"]

    monkeypatch.setattr("shared.port_preflight.process_mentions", _mentions)

    def _terminate(pid: int, *, label: str, timeout_s: float = 5.0) -> bool:
        state["killed"].append(pid)
        return True

    monkeypatch.setattr("cli.commands._pgbouncer._terminate_verified", _terminate)
    return state


def _ctx(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    return repo, home


def _detached_listener(tmp_path: Path, *, identity: Path) -> tuple[int, int]:
    """Start a real TCP listener whose final argv token states its identity.

    The launcher exits immediately so the listener is not pytest's child. That
    matches the orphan daemon shape and lets the verified-kill liveness check
    observe a gone process rather than an unreaped child zombie.
    """
    ready = tmp_path / f"listener-{identity.name}.port"
    child = (
        "import pathlib, socket, sys, time\n"
        "sock = socket.socket()\n"
        "sock.bind(('127.0.0.1', 0))\n"
        "sock.listen()\n"
        f"pathlib.Path({str(ready)!r}).write_text(str(sock.getsockname()[1]))\n"
        "while True: time.sleep(60)\n"
    )
    launcher = (
        "import subprocess, sys\n"
        f"p = subprocess.Popen([sys.executable, '-c', {child!r}, {str(identity)!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print(p.pid)\n"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", launcher],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    pid = int(done.stdout.strip())
    deadline = time.monotonic() + 30
    while not ready.exists():
        assert time.monotonic() < deadline, "the listener fixture never became ready"
        time.sleep(0.02)
    return pid, int(ready.read_text())


def _kill_fixture_process(pid: int) -> None:
    with contextlib.suppress(psutil.Error):
        proc = psutil.Process(pid)
        proc.kill()
        proc.wait(timeout=5)


def test_destroy_reaper_leaves_foreign_listener_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression #1426: the checkout running a cross-home destroy is not an
    ownership token. A foreign listener launched by that same Python checkout
    survives, and the refusal identifies the port, process, and pid."""
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    home.mkdir()
    pid, port = _detached_listener(
        tmp_path,
        identity=Path(f"{home}-foreign") / "source" / ".venv" / "bin" / "python",
    )
    monkeypatch.setattr(
        "shared.port_preflight.unit_port_map",
        lambda _home: {"gateway": port},  # pyright: ignore[reportUnknownArgumentType]
    )

    try:
        assert _or._reap_orphan_listeners(repo, home, preserve=frozenset()) == []
        assert psutil.pid_exists(pid), "destroy killed a listener it could not attribute"
        out = capsys.readouterr().out
        assert f"not claiming {port}: belongs to" in out
        assert f"(pid {pid})" in out
    finally:
        _kill_fixture_process(pid)


def test_destroy_reaper_reaps_listener_identified_by_cluster_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listener whose argv positively names the target cluster home remains
    reapable, so the ownership guard does not turn destroy into a blanket skip."""
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    home.mkdir()
    pid, port = _detached_listener(tmp_path, identity=home / "source" / ".venv" / "bin" / "python")
    monkeypatch.setattr(
        "shared.port_preflight.unit_port_map",
        lambda _home: {"gateway": port},  # pyright: ignore[reportUnknownArgumentType]
    )

    try:
        assert _or._reap_orphan_listeners(repo, home, preserve=frozenset()) == [
            ("gateway", port, pid)
        ]
        assert not psutil.pid_exists(pid)
    finally:
        _kill_fixture_process(pid)


def test_reap_kills_our_listeners_and_leaves_foreign(
    tmp_path: Path, sweep_seams: dict[str, list[int]]
) -> None:
    """An OUR listener on a unit port is an orphan of the just-stopped service
    and is killed verified; a FOREIGN listener (another unit's or the
    operator's process) is never touched."""
    repo, home = _ctx(tmp_path)
    sweep_seams["ours"] = [101]

    reaped = _or._reap_orphan_listeners(repo, home, preserve=frozenset())

    assert reaped == [("gateway", 8000, 101)]
    assert sweep_seams["killed"] == [101]


def test_reap_preserved_services_are_never_touched(
    tmp_path: Path, sweep_seams: dict[str, list[int]]
) -> None:
    """Preserved services (the browser on keep_browser, the data plane on
    keep_infra, an explicitly preserved session) hold their ports
    LEGITIMATELY — even an ours-listener on them is not reaped."""
    repo, home = _ctx(tmp_path)
    sweep_seams["ours"] = [101, 303]

    reaped = _or._reap_orphan_listeners(repo, home, preserve=frozenset({"browser"}))

    assert reaped == [("gateway", 8000, 101)]
    assert sweep_seams["killed"] == [101]


def test_reap_surviving_pid_is_not_claimed(
    tmp_path: Path, sweep_seams: dict[str, list[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pid that survives even SIGKILL is NOT counted as reaped — the sweep
    must never claim a closure it did not achieve (the stop must not read as
    done when an old process still holds the port)."""
    repo, home = _ctx(tmp_path)
    sweep_seams["ours"] = [101]

    monkeypatch.setattr(
        "cli.commands._pgbouncer._terminate_verified",
        lambda _pid, **_kw: False,  # pyright: ignore[reportUnknownArgumentType]
    )

    reaped = _or._reap_orphan_listeners(repo, home, preserve=frozenset())

    assert reaped == []
    assert sweep_seams["killed"] == []


def test_reap_orphan_step_preserves_browser_and_infra(
    tmp_path: Path, sweep_seams: dict[str, list[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The step composes the preserve set from the stop's own flags: the
    browser on keep_browser, the data plane on keep_infra, the always-up gate
    on keep_gate, and explicitly preserved sessions (cmd_update keeps the
    frontend serving through a backend-only bounce)."""
    repo, home = _ctx(tmp_path)
    sweep_seams["ours"] = [101, 202, 303]
    monkeypatch.setattr(_or, "ava_home", lambda: home)

    class _Spec:
        session = "frontend"

    monkeypatch.setattr(_or, "build_services", lambda: (_Spec(),))

    # Backend-only rollout: keep_browser=True + keep_infra=True +
    # keep_gate=True + preserve_sessions={'frontend'} (the BARE service name
    # `_run_gateway_local_update` passes, matching `_compute_stop_scope`).
    # browser / postgres / redis / pgbouncer / the gate's "frontend" port /
    # the Next.js "app" port (the frontend session's real port key) preserved.
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        _or,
        "_reap_orphan_listeners",
        lambda _r, _h, *, preserve: seen.update(preserve=preserve) or [],  # pyright: ignore[reportUnknownArgumentType]
    )
    _stop._reap_orphan_step(
        repo,
        keep_browser=True,
        keep_infra=True,
        preserve_sessions=frozenset({"frontend"}),
        keep_gate=True,
    )
    assert seen["preserve"] == frozenset(
        {"browser", "postgres", "redis", "pgbouncer", "frontend", "app"}
    )


def test_reap_orphan_step_full_stop_reaps_gate_and_app(
    tmp_path: Path, sweep_seams: dict[str, list[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full stop (teardown_extras=True -> keep_gate=False, keep_infra=False)
    preserves only the browser: the gate was torn down by stop_gate_service and
    the frontend by the session kill, so any listener still on their ports is
    an orphan this sweep must close."""
    repo, home = _ctx(tmp_path)
    monkeypatch.setattr(_or, "ava_home", lambda: home)
    monkeypatch.setattr(_or, "build_services", lambda: ())

    seen: dict[str, object] = {}
    monkeypatch.setattr(
        _or,
        "_reap_orphan_listeners",
        lambda _r, _h, *, preserve: seen.update(preserve=preserve) or [],  # pyright: ignore[reportUnknownArgumentType]
    )
    _stop._reap_orphan_step(
        repo,
        keep_browser=True,
        keep_infra=False,
        preserve_sessions=frozenset(),
        keep_gate=False,
    )
    assert seen["preserve"] == frozenset({"browser"})


def test_reap_orphan_step_full_rollout_keeps_gate_reaps_app(
    tmp_path: Path, sweep_seams: dict[str, list[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full rollout (restart_frontend=True -> preserve_sessions empty) keeps
    the gate live but NOT the app: the frontend is rebuilt on the new code by
    `ava start`, so its old listener is an orphan to close. The gate has no
    rebuild — it must stay (teardown_extras=False by construction)."""
    repo, home = _ctx(tmp_path)
    monkeypatch.setattr(_or, "ava_home", lambda: home)
    monkeypatch.setattr(_or, "build_services", lambda: ())

    seen: dict[str, object] = {}
    monkeypatch.setattr(
        _or,
        "_reap_orphan_listeners",
        lambda _r, _h, *, preserve: seen.update(preserve=preserve) or [],  # pyright: ignore[reportUnknownArgumentType]
    )
    _stop._reap_orphan_step(
        repo,
        keep_browser=True,
        keep_infra=True,
        preserve_sessions=frozenset(),
        keep_gate=True,
    )
    assert seen["preserve"] == frozenset({"browser", "postgres", "redis", "pgbouncer", "frontend"})

"""Updater/spawn environment isolation — the 2026-08-27 incident, locked.

The incident (P0, Task #1828): an updater test spawned a subprocess that tried to
isolate with `os.environ.setdefault("AVA_HOME", "/tmp/ava-test-home")`. The
subprocess inherited the operator's production AVA_HOME, so the setdefault was a
no-op; the fake session backend then called the REAL `cluster_deploy.spawn_update(...)`,
which wrote the pending updater handoff (`$AVA_HOME/run/updater-handoff.json`)
and paused the production host (host_deploy_state posture row) before the fake
backend's AttributeError — Gateway 503 until a human recovered the host.

This module locks the three halves of the fix:

- the suite redirects AVA_HOME by ASSIGNMENT (never setdefault) and nothing the
  updater machinery resolves — in-process or in a forwarded child env — can be
  the production home;
- the deploy triggers (`spawn_update` / `spawn_rollout` / `spawn_restart`)
  refuse the production home from any checkout that
  is not its anchored `~/.ava/source`, BEFORE any handoff write, posture write,
  pause, or session spawn;
- a failed spawn — even at the real-code level in a child process — leaves no
  pending handoff and no pause anywhere, and never touches the production
  handoff file.

Every test in this module is the subject under test (the spawn family), so the
autouse `_guard_cluster_spawn` safety net is opted out per its contract: the
write seams are stubbed here instead, with calls recorded.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from ops import cluster_deploy
from ops.deploy_spawn import ProdHomeFromForeignCheckout
from ops.update_check import UpdateCheck
from shared.dotenv_boot import AVA_ENV_PATH
from shared.paths import ava_home

pytestmark = pytest.mark.real_cluster_spawn

_PROD_HOME = Path.home() / ".ava"


class _Inactive:
    """A `shared.ui_update_state` snapshot whose status is `inactive`."""

    status = "inactive"
    generation = None
    kind = None
    legacy = False
    origin = None


# ─── half 1: the suite's home redirect is an assignment, never production ────


def test_suite_home_redirect_is_an_assignment_and_never_production() -> None:
    """The suite pins AVA_HOME by assignment (tests/conftest.py), so every test
    process — and every subprocess it spawns by env inheritance — resolves the
    tmpfs test home. A setdefault here would silently keep a leaked production
    AVA_HOME (the 2026-08-27 incident's exact mechanism)."""
    env_home = os.environ.get("AVA_HOME")
    assert env_home, "tests/conftest.py must pin AVA_HOME by assignment"
    assert Path(env_home).expanduser() != _PROD_HOME
    assert ava_home() != _PROD_HOME
    assert Path(AVA_ENV_PATH).parent == ava_home()


def test_forwarded_session_env_carries_the_isolated_home() -> None:
    """The env a detached updater/rollout/restart child receives
    (`shared.session_env.forward_env_dict` — the session-forward allowlist)
    carries the suite's isolated home, never the operator's production home.
    Locks the child-inheritance half of the incident at the forwarding
    mechanism."""
    from shared.session_env import forward_env_dict

    child = forward_env_dict()
    assert child.get("AVA_HOME") == str(ava_home())
    assert child.get("AVA_HOME") != str(_PROD_HOME)


# ─── half 2: the deploy triggers refuse the production home from this checkout ─


def _stub_all_deploy_write_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, list[object]]:
    """Stub every write seam the deploy triggers can reach, recording calls.

    The production home is made to resolve to the operator's real `~/.ava`; the
    guard must fire before ANY of these runs. The stubs make the test safe even
    if the guard regresses — no real production side effect is possible.
    """
    records: dict[str, list[object]] = {
        "begin": [],
        "pause": [],
        "posture": [],
        "respawn": [],
        "spawn": [],
        "log": [],
    }
    monkeypatch.setattr("shared.paths.ava_home", lambda: _PROD_HOME)
    monkeypatch.setattr(
        "shared.updater_handoff.begin",
        lambda **_kw: records["begin"].append(_kw),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        "ops.cluster_pause.pause_local_cluster",
        lambda: records["pause"].append(True),  # pyright: ignore[reportUnknownArgumentType]
    )

    def _record_posture(posture: str) -> None:
        records["posture"].append(posture)

    monkeypatch.setattr("shared.host_deploy_state.set_posture", _record_posture)

    class _Backend:
        def has_session(self, _name: str) -> bool:
            return True

        def new_session(
            self, name: str, _cmd: str, _cwd: object, *, env: object, **_k: object
        ) -> bool:
            records["respawn"].append(name)
            return True

        def kill_session(
            self, _name: str, graceful: bool = False, expected: bool = False
        ) -> tuple[bool, str]:
            return True, "forced"

    def _stub_backend() -> _Backend:
        return _Backend()

    monkeypatch.setattr("shared.session_backend.get_backend", _stub_backend)
    monkeypatch.setattr("shared.disabled_services.read_skipped", dict)
    monkeypatch.setattr(
        "ops.cluster_session._spawn_detached_session",
        lambda *_a, **_k: records["spawn"].append(_k),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        "ops.cluster_deploy._new_update_log",
        lambda _prefix: _PROD_HOME / "logs" / "probe.log",  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        "ops.cluster_deploy._wait_for_ui_owner",
        lambda **_k: None,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(  # type: ignore[func-returns-value]
        "ops.cluster_deploy.update_check",
        lambda: UpdateCheck(
            behind=2, frontend_changed=False, backend_changed=True, needs_replay=False
        ),
    )
    monkeypatch.setattr(
        "ops.cluster_session._has_orchestration_session",
        lambda _n: False,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr("ops.cluster_session.live_orchestration_session", lambda: None)
    monkeypatch.setattr("shared.ui_update_state.read", _Inactive)
    monkeypatch.setattr("shared.ui_update_state.lifecycle_lock", contextlib.nullcontext)
    return records


def _drive_update(_monkeypatch: pytest.MonkeyPatch) -> None:
    cluster_deploy.spawn_update(restart_only=True)


def _drive_rollout(_monkeypatch: pytest.MonkeyPatch) -> None:
    cluster_deploy.spawn_rollout("test-origin")


def _drive_restart(_monkeypatch: pytest.MonkeyPatch) -> None:
    cluster_deploy.spawn_restart("test-origin")


_DEPLOY_TRIGGERS: tuple[tuple[str, Callable[[pytest.MonkeyPatch], None]], ...] = (
    ("update", _drive_update),
    ("rollout", _drive_rollout),
    ("restart", _drive_restart),
)


@pytest.mark.parametrize(
    ("label", "drive"),
    _DEPLOY_TRIGGERS,
    ids=["update", "rollout", "restart"],
)
def test_deploy_triggers_refuse_the_production_home_from_this_checkout(
    label: str,
    drive: Callable[[pytest.MonkeyPatch], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deploy triggers refuse when the
    resolved home is the production home but the executing checkout is not its
    anchored `~/.ava/source` — the exact 2026-08-27 incident shape (a dev/test
    checkout inheriting production AVA_HOME). The refusal fires BEFORE any write
    seam: no handoff begin, no pause, no posture write, no service spawn,
    no session spawn, not even the update-log dir creation.

    A non-prod home (a worktree cluster, the suite's tmpfs home) never trips
    this guard — the rest of the suite's spawn tests and the child-process test
    below exercise exactly that pass-through."""
    records = _stub_all_deploy_write_seams(monkeypatch)

    with pytest.raises(ProdHomeFromForeignCheckout) as exc_info:  # pyright: ignore[reportUnknownArgumentType]
        drive(monkeypatch)

    message = str(exc_info.value)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    # `prod_service_checkout_error` renders the anchored checkout as an ABSOLUTE
    # path (Path.home()/".ava"/"source") — never the `~` spelling.
    assert str(_PROD_HOME / "source") in message
    assert records == {
        "begin": [],
        "pause": [],
        "posture": [],
        "respawn": [],
        "spawn": [],
        "log": [],
    }, (
        f"{label}: the guard must fire before any handoff write, pause, posture "
        "write, service spawn, session spawn, or log creation"
    )


# ─── half 3: a failed spawn leaves no handoff and no pause, anywhere ─────────


def test_failed_spawn_in_a_child_process_leaves_no_handoff_and_no_pause(
    tmp_path: Path,
) -> None:
    """Drive the REAL `spawn_update` failure path in a child process.

    The child isolates by ASSIGNMENT (`os.environ["AVA_HOME"] = ...`), never
    setdefault — the incident's exact mistake — against a deliberately fresh
    home, and the session backend declines the spawn mid-transaction (the
    incident's fake-backend failure class). Locks the negative property at the
    real-code level:

    - no pending handoff survives the failure (`updater_handoff.read()` is
      inactive) and no admission hold survives;
    - nothing outside the child's own home is touched: the modeled production
      handoff file (under the child's fake HOME, audit M-1) is byte-identical
      before and after.

    The child's home also passes the prod-home checkout guard (non-prod home),
    proving the guard does not block legitimate non-prod deploy targets."""
    from shared.paths import repo_root

    scratch = tmp_path / "child-home"
    scratch.mkdir()
    # The child home must declare the cluster keys in its own .env or the
    # env-authority pass drops them (same shape as tests/conftest.py plants the
    # suite home's .env).
    shutil.copy(ava_home() / ".env", scratch / ".env")

    # The "production" home the child must never touch is modeled, not the
    # operator's real ~/.ava (audit M-1): the child runs with HOME=fake_home,
    # so any Path.home()/.ava write lands here, where the sentinel catches it —
    # and the test no longer reads (or depends on) the operator's real handoff
    # file, which can change mid-run during a real update.
    fake_home = tmp_path / "fake-home"
    prod_handoff = fake_home / ".ava" / "run" / "updater-handoff.json"
    prod_handoff.parent.mkdir(parents=True)
    prod_handoff.write_text("sentinel: the updater machinery must never touch this file\n")
    before = prod_handoff.read_bytes()

    script = textwrap.dedent(f"""        import json, os, sys

        # The isolation under test: ASSIGN, never setdefault (2026-08-27 incident).
        os.environ["AVA_HOME"] = {str(scratch)!r}
        os.environ["AVA_HOME_OVERRIDE"] = "1"
        sys.path.insert(0, {str(repo_root())!r})

        import shared.host_deploy_state
        import shared.session_backend
        import shared.updater_handoff
        import shared.maintenance
        from shared.machine import set_identity
        set_identity(name="isolated-updater-child", role="agent-runner")
        from ops import cluster_deploy, cluster_session
        from ops.cluster_session import OrchestrationSpawnFailed

        posture_writes: list[str] = []
        shared.host_deploy_state.set_posture = lambda p: posture_writes.append(p)  # type: ignore[method-assign]

        class _Backend:
            def has_session(self, name: str) -> bool:
                return False  # No native host exists in this private home.

            def kill_session(self, name, *, graceful=False, expected=False):
                return True, "forced"

        shared.session_backend.get_backend = lambda: _Backend()  # type: ignore[method-assign]
        cluster_session._has_orchestration_session = lambda _name: False  # type: ignore[method-assign]

        def _decline(*_a, **_k):
            raise OrchestrationSpawnFailed("backend declined", started=False)

        cluster_session._spawn_detached_session = _decline  # type: ignore[method-assign]

        try:
            cluster_deploy.spawn_update(restart_only=True)
            error = None
        except Exception as exc:
            error = type(exc).__name__

        snapshot = shared.updater_handoff.read()
        print(json.dumps({{
            "handoff": snapshot.status,
            "posture_writes": posture_writes,
            "error": error,
            "held": shared.maintenance.held(),
        }}))
    """)
    # S603: the child command is a fixed script this test itself wrote (no
    # untrusted input); it runs the repo's own spawn machinery with stubbed seams.
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={
            **os.environ,
            "HOME": str(fake_home),
            "AVA_HOME": str(scratch),
            "AVA_HOME_OVERRIDE": "1",
        },
    )
    assert done.returncode == 0, done.stderr[-2000:]
    payload = json.loads(done.stdout.strip().splitlines()[-1])
    assert payload["error"] == "OrchestrationSpawnFailed"
    assert payload["handoff"] == "inactive", "a failed spawn must clear its pending handoff"
    assert not payload["held"], "a failed spawn must release its admission hold"
    assert payload["posture_writes"] == ["idle"], (
        "a failed spawn must undo its pause (the incident left production paused)"
    )

    after = prod_handoff.read_bytes()
    assert after == before, "the updater machinery must never touch the production handoff file"

"""Every `ops.cluster` spawn site must reach the platform session backend.

The orchestration spawns (`spawn_update`, `spawn_rollout`, `spawn_restart`)
share the platform's session backend. Native
drain retains services; resume releases admission without creating a service.
Windows commands use cmd.exe syntax and POSIX commands use shell syntax.
The tests swap the platform backend rather than the host, so they run anywhere.
"""

from __future__ import annotations

import logging
import subprocess as _sp
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops import cluster as cluster_mod
from ops import cluster_deploy
from ops import updater_outcome as uo
from shared.config import settings
from shared.exit_codes import RESTART_DECLINED_EXIT_CODE
from shared.platform_backend import MacPlatformBackend, WindowsPlatformBackend

_REAL_WAIT_FOR_UI_OWNER = cluster_deploy._wait_for_ui_owner


class _FakeSessionBackend:
    """Records `new_session` instead of launching anything."""

    def __init__(self) -> None:
        self.spawned: list[tuple[str, str, Path]] = []
        self.alive: set[str] = set()

    def has_session(self, name: str) -> bool:
        return name in self.alive

    def new_session(
        self,
        name: str,
        cmd: str,
        cwd: Path,
        *,
        env: dict[str, str],
        login_shell: bool = True,
        exec_cmd: bool = True,
    ) -> bool:
        assert env, "the native spawn must hand the child a populated env"
        # The child env is the session-forward view (shared.session_env /
        # shared.env_registry), which carries AVA_HOME from the SUITE's pinned
        # test home — never the operator's production home. A child that
        # inherited production AVA_HOME is exactly the 2026-08-27 incident: the
        # updater test subprocess wrote the pending handoff and paused the
        # production host (Gateway 503). Lock the child-env property here, where
        # every spawn-site test passes through this one backend.
        assert env.get("AVA_HOME") == str(settings.general.ava_home), (
            f"child env must carry the suite's isolated home "
            f"({settings.general.ava_home}), got AVA_HOME={env.get('AVA_HOME')!r}"
        )
        assert env.get("AVA_HOME") != str(Path.home() / ".ava"), (
            "child env must never carry the production home"
        )
        self.spawned.append((name, cmd, cwd))
        self.alive.add(name)
        return True

    def kill_session(
        self, name: str, *, graceful: bool = False, timeout: float = 15.0, expected: bool = False
    ) -> tuple[bool, str]:
        self.alive.discard(name)
        return True, "forced"

    def list_sessions(self, prefix: str = "") -> list[str]:
        return sorted(n for n in self.alive if n.startswith(prefix))


def _successful_fetch(
    _argv: Sequence[str],
    *,
    timeout: float,
    capture_output: bool = False,
    **popen_kwargs: object,
) -> _sp.CompletedProcess[str]:
    """Return the fetch result irrelevant to session-spawn shape tests."""
    return _sp.CompletedProcess(["git", "fetch", "origin"], 0, "", "")


def _failed_fetch(
    _argv: Sequence[str],
    *,
    timeout: float,
    capture_output: bool = False,
    **popen_kwargs: object,
) -> _sp.CompletedProcess[str]:
    """Return a failed fetch so the warning path remains observable."""
    return _sp.CompletedProcess(
        ["git", "fetch", "origin"], 128, "", "fatal: unable to access origin"
    )


@pytest.fixture(autouse=True)
def _pin_session_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic session names + empty env forwarding, matching
    tests/gateway/test_cluster_endpoints.py."""
    monkeypatch.setattr("shared.cluster.session_name", lambda svc: f"ava-test-{svc}")  # pyright: ignore[reportUnknownArgumentType]


@pytest.fixture(autouse=True)
def _detached_child_publishes_ui_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recording backend does not execute its child command.

    Model the authoritative child claim at the parent's wait seam; focused
    tests below exercise the wait's liveness/timeout behavior itself.
    """

    def _publish(*, session: str, kind: str, origin: str) -> None:
        del session
        from shared import ui_update_state

        if ui_update_state.read().status == "inactive":
            ui_update_state.begin(kind=kind, origin=origin)  # type: ignore[arg-type]

    monkeypatch.setattr(cluster_deploy, "_wait_for_ui_owner", _publish)


@pytest.fixture
def native_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[_FakeSessionBackend]:
    """A Windows host: Windows platform backend + a recording session backend.

    Any `subprocess.run` from this module's spawn paths is a hard failure —
    sessions must go through the session backend, never a raw binary. That is
    the bug under test.
    """
    backend = _FakeSessionBackend()
    monkeypatch.setattr("shared.platform_backend.get_backend", WindowsPlatformBackend)
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: backend)
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)

    def _run(argv: list[str], *a: object, **k: object) -> _sp.CompletedProcess[str]:
        raise AssertionError(
            f"spawn path must never shell out — sessions go through the backend: {argv}"
        )

    monkeypatch.setattr(cluster_deploy.subprocess, "run", _run)
    monkeypatch.setattr(cluster_deploy, "run_bounded", _successful_fetch)
    monkeypatch.setattr("shared.migrations.validate_migrations_at_ref", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
    yield backend


@pytest.fixture
def posix_native_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[_FakeSessionBackend]:
    """A POSIX host post-switch: every session — services AND orchestration —
    lives on the native supervisor (session backend); agent shells live on the
    PTY supervisor. Any raw binary spawn from any spawn path is a hard failure
    — that is the bug this state exists to catch (a session spawned outside the
    backend while start/healthcheck use it would double-run).
    """
    backend = _FakeSessionBackend()
    monkeypatch.setattr("shared.platform_backend.get_backend", MacPlatformBackend)
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: backend)
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)

    def _run(argv: list[str], *a: object, **k: object) -> _sp.CompletedProcess[str]:
        raise AssertionError(
            f"spawn path must never shell out — sessions go through the backend: {argv}"
        )

    monkeypatch.setattr(cluster_deploy.subprocess, "run", _run)
    monkeypatch.setattr(cluster_deploy, "run_bounded", _successful_fetch)
    yield backend


def _drive_unpause() -> None:
    from shared import maintenance, pause_owner

    cluster_mod.pause_local_cluster()
    paused = pause_owner.read()
    assert paused.maintenance is not None and paused.maintenance.phase == "drained"
    cluster_mod.unpause_local_cluster()
    assert not maintenance.held()
    assert pause_owner.read().status == "resumed"


def _drive_update() -> None:
    cluster_mod.spawn_update()


def _drive_update_restart_only() -> None:
    cluster_mod.spawn_update(restart_only=True)


def _drive_rollout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cluster_deploy,
        "update_check",
        lambda: cluster_mod.UpdateCheck(
            behind=2, frontend_changed=False, backend_changed=True, needs_replay=False
        ),
    )
    cluster_mod.spawn_rollout("test-origin")


@pytest.mark.real_cluster_spawn
def test_dry_run_rollout_uses_its_own_session_and_does_not_wait_for_a_ui_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dry run is detached separately and has no maintenance owner to await."""
    monkeypatch.setattr(
        cluster_deploy,
        "update_check",
        lambda: cluster_mod.UpdateCheck(
            behind=2, frontend_changed=False, backend_changed=True, needs_replay=False
        ),
    )
    monkeypatch.setattr(cluster_deploy, "_assert_no_orchestration_in_flight", lambda **_kw: None)  # pyright: ignore[reportUnknownArgumentType]

    def _rollout_log(_kind: str) -> Path:
        return tmp_path / "rollout.log"

    monkeypatch.setattr(cluster_deploy, "_new_update_log", _rollout_log)
    monkeypatch.setattr(cluster_deploy.shared.ui_update_state, "lifecycle_lock", nullcontext)
    monkeypatch.setattr(
        cluster_deploy.shared.ui_update_state,
        "read",
        lambda: SimpleNamespace(status="inactive"),
    )
    spawned: list[tuple[str, str]] = []

    def _capture_spawn(session: str, *, shell_cmd: str, native_cmd: str) -> None:
        del native_cmd
        spawned.append((session, shell_cmd))

    monkeypatch.setattr(
        cluster_deploy.cluster_session,
        "_spawn_detached_session",
        _capture_spawn,
    )
    waited: list[dict[str, str]] = []
    monkeypatch.setattr(
        cluster_deploy,
        "_wait_for_ui_owner",
        lambda **kwargs: waited.append(kwargs),  # pyright: ignore[reportUnknownArgumentType]
    )

    result = cluster_mod.spawn_rollout("test-origin", dry_run=True)

    assert result["session"] == "ava-test-rollout-dryrun"
    assert spawned[0][0] == "ava-test-rollout-dryrun"
    assert "--dry-run" in spawned[0][1]
    assert waited == []


@pytest.mark.real_cluster_spawn
def test_normal_rollout_waits_for_its_ui_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A committing rollout still publishes its maintenance owner before returning."""
    monkeypatch.setattr(
        cluster_deploy,
        "update_check",
        lambda: cluster_mod.UpdateCheck(
            behind=2, frontend_changed=False, backend_changed=True, needs_replay=False
        ),
    )
    monkeypatch.setattr(cluster_deploy, "_assert_no_orchestration_in_flight", lambda **_kw: None)  # pyright: ignore[reportUnknownArgumentType]

    def _rollout_log(_kind: str) -> Path:
        return tmp_path / "rollout.log"

    monkeypatch.setattr(cluster_deploy, "_new_update_log", _rollout_log)
    monkeypatch.setattr(cluster_deploy.shared.ui_update_state, "lifecycle_lock", nullcontext)
    monkeypatch.setattr(
        cluster_deploy.shared.ui_update_state,
        "read",
        lambda: SimpleNamespace(status="inactive"),
    )
    spawned: list[str] = []
    monkeypatch.setattr(
        cluster_deploy.cluster_session,
        "_spawn_detached_session",
        lambda session, **_kwargs: spawned.append(session),  # pyright: ignore[reportUnknownArgumentType]
    )
    waited: list[dict[str, str]] = []
    monkeypatch.setattr(
        cluster_deploy,
        "_wait_for_ui_owner",
        lambda **kwargs: waited.append(kwargs),  # pyright: ignore[reportUnknownArgumentType]
    )

    result = cluster_mod.spawn_rollout("test-origin")

    assert result["session"] == "ava-test-rollout"
    assert spawned == ["ava-test-rollout"]
    assert waited == [{"session": "ava-test-rollout", "kind": "rollout", "origin": "test-origin"}]


def _drive_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster_mod.spawn_restart("test-origin")


# (label, driver, expected session, substring the native command must contain)
_SPAWN_SITES: tuple[tuple[str, Callable[[pytest.MonkeyPatch], None], str, str], ...] = (
    ("update", lambda _mp: _drive_update(), "ava-test-updater", "git checkout --force"),
    (
        "update-restart-only",
        lambda _mp: _drive_update_restart_only(),
        "ava-test-updater",
        # `ava restart --quiesce --mode smooth` (the default drain: signal this
        # host's agents, wait out long execs, force-reap stragglers) plus the
        # errorlevel ladder that keeps a DECLINED restart (host still serving)
        # from being started over — see _restart_recovery_cmd.
        "ava restart --quiesce --mode smooth & if errorlevel",
    ),
    ("rollout", _drive_rollout, "ava-test-rollout", "ava cluster update --local"),
    (
        "restart",
        _drive_restart,
        "ava-test-cluster-restart",
        "ava cluster update --local --restart-only",
    ),
)


@pytest.mark.real_cluster_spawn
@pytest.mark.parametrize(
    ("label", "drive", "session", "expected"),
    _SPAWN_SITES,
    ids=[s[0] for s in _SPAWN_SITES],
)
def test_spawn_site_reaches_session_backend(
    label: str,
    drive: Callable[[pytest.MonkeyPatch], None],
    session: str,
    expected: str,
    native_host: _FakeSessionBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each spawn entry point launches its session through the session backend on a
    host with no raw spawn — this is the regression the four hardcoded
    binary-spawn argvs caused."""
    drive(monkeypatch)

    assert [s[0] for s in native_host.spawned] == [session], f"{label} spawned the wrong session"
    name, cmd, cwd = native_host.spawned[0]
    assert name == session
    assert expected in cmd
    assert cwd == cluster_mod._REPO_ROOT
    # The POSIX-only shapes must not leak into a cmd.exe command line.
    for posix_only in ("tee -a", "$SHELL", "2>&1 |", "; fi", "$?"):
        assert posix_only not in cmd, f"{label}: {posix_only!r} is not runnable by cmd.exe"


@pytest.mark.real_cluster_spawn
@pytest.mark.parametrize(
    ("drive", "kind"),
    [(_drive_rollout, "rollout"), (_drive_restart, "restart")],
)
def test_cluster_ui_marker_exists_before_detached_spawn_returns(
    drive: Callable[[pytest.MonkeyPatch], None],
    kind: str,
    posix_native_host: _FakeSessionBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The browser may receive the start SSE as soon as the spawn returns, so
    the durable marker must already own the maintenance surface."""
    drive(monkeypatch)

    from shared.ui_update_state import read

    snapshot = read()
    assert snapshot.status == "updating"
    assert snapshot.kind == kind
    assert snapshot.generation is not None


@pytest.mark.real_cluster_spawn
def test_definitive_rollout_spawn_decline_never_creates_a_marker(
    posix_native_host: _FakeSessionBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ops.cluster_session import OrchestrationSpawnFailed
    from shared.ui_update_state import read

    monkeypatch.setattr(
        cluster_deploy,
        "update_check",
        lambda: cluster_mod.UpdateCheck(
            behind=2, frontend_changed=False, backend_changed=True, needs_replay=False
        ),
    )
    posix_native_host.new_session = lambda *_a, **_kw: False  # type: ignore[method-assign]

    with pytest.raises(OrchestrationSpawnFailed):
        cluster_mod.spawn_rollout("test-origin")

    assert read().status == "inactive"


@pytest.mark.real_cluster_spawn
def test_second_spawn_never_reuses_or_clears_the_first_generation(
    posix_native_host: _FakeSessionBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second parent that observes a child-owned marker refuses unchanged."""
    _drive_rollout(monkeypatch)

    from shared.ui_update_state import read

    first = read()
    assert first.generation is not None
    # Model the session record disappearing while its durable owner survives.
    posix_native_host.alive.clear()

    with pytest.raises(cluster_mod.ClusterUpdateInProgress):
        cluster_mod.spawn_restart("second-caller")

    after = read()
    assert after.generation == first.generation
    assert after.kind == "rollout"


@pytest.mark.real_cluster_spawn
def test_ambiguous_post_launch_failure_never_clears_child_owned_marker(
    posix_native_host: _FakeSessionBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Popen/fork can succeed before session-record bookkeeping raises.

    The parent never owns a marker and therefore cannot clear the child state
    merely because its backend call reported an ambiguous failure.
    """
    from ops.cluster_session import OrchestrationSpawnFailed
    from shared import ui_update_state

    monkeypatch.setattr(
        cluster_deploy,
        "update_check",
        lambda: cluster_mod.UpdateCheck(
            behind=2, frontend_changed=False, backend_changed=True, needs_replay=False
        ),
    )

    def _child_started_then_record_failed(*_args: object, **_kwargs: object) -> None:
        ui_update_state.begin(kind="rollout", origin="first-caller")
        raise OrchestrationSpawnFailed("injected post-launch record failure", started=None)

    monkeypatch.setattr(
        cluster_deploy.cluster_session,
        "_spawn_detached_session",
        _child_started_then_record_failed,
    )

    with pytest.raises(OrchestrationSpawnFailed):
        cluster_mod.spawn_rollout("first-caller")

    remaining = ui_update_state.read()
    assert remaining.status == "updating"
    assert remaining.kind == "rollout"
    assert remaining.origin == "first-caller"


@pytest.mark.real_cluster_spawn
def test_parent_wait_accepts_a_slow_live_child_that_publishes_after_five_seconds(
    posix_native_host: _FakeSessionBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old arbitrary 5s wait falsely reported failure on a slow DB lock."""
    from shared import ui_update_state

    clock = [0.0]
    posix_native_host.alive.add("ava-test-rollout")
    monkeypatch.setattr(cluster_deploy.time, "monotonic", lambda: clock[0])

    def _advance(delay: float) -> None:
        clock[0] += max(delay, 1.0)
        if clock[0] >= 6.0 and ui_update_state.read().status == "inactive":
            ui_update_state.begin(kind="rollout", origin="slow-db")

    monkeypatch.setattr(cluster_deploy.time, "sleep", _advance)

    _REAL_WAIT_FOR_UI_OWNER(session="ava-test-rollout", kind="rollout", origin="slow-db")
    assert clock[0] >= 6.0


@pytest.mark.real_cluster_spawn
def test_parent_wait_fails_immediately_when_the_session_dies(
    posix_native_host: _FakeSessionBackend,
) -> None:
    from ops.cluster_session import OrchestrationSpawnFailed

    with pytest.raises(OrchestrationSpawnFailed, match="exited before publishing"):
        _REAL_WAIT_FOR_UI_OWNER(session="ava-test-rollout", kind="rollout", origin="dead-child")


@pytest.mark.real_cluster_spawn
def test_unpause_releases_admission_without_spawning_services(
    posix_native_host: _FakeSessionBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ops import agent_pause

    monkeypatch.setattr(agent_pause, "host_running", lambda: False)
    _drive_unpause()
    assert posix_native_host.spawned == []


@pytest.mark.real_cluster_spawn
@pytest.mark.parametrize("restart_only", [False, True], ids=["update", "restart-only"])
def test_the_native_updater_command_opens_with_this_runs_start_marker(
    restart_only: bool, native_host: _FakeSessionBackend
) -> None:
    """The native supervisor appends every run to ONE log, so `ops.updater_outcome`
    has nothing to tell one run's lines from the next's — and read a previous run's
    decline as this run's verdict (issue #1117).

    The marker is echoed FIRST, ahead of the `cd` and the checkout, so it is the run's
    first written line whatever the command does next; a marker printed after a step
    that can fail would be missing from exactly the runs worth diagnosing. Both
    updater shapes get it — a restart-only bounce appends to the same log.
    """
    cluster_mod.spawn_update(restart_only=restart_only)

    _, cmd, _ = native_host.spawned[0]
    head, _, rest = cmd.partition(" & ")
    assert uo._marker_epoch(head.removeprefix("echo ")) == pytest.approx(time.time(), abs=60)  # pyright: ignore[reportUnknownMemberType]
    assert rest, "the marker must head the real command, not replace it"


@pytest.mark.real_cluster_spawn
@pytest.mark.parametrize("restart_only", [False, True], ids=["update", "restart-only"])
def test_the_posix_updater_command_marks_the_authoritative_wall_time_boundary(
    posix_native_host: _FakeSessionBackend,
    restart_only: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh POSIX log still needs a durable endpoint-paired wall-time start."""
    monkeypatch.setattr("shared.migrations.validate_migrations_at_ref", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
    cluster_mod.spawn_update(restart_only=restart_only)

    cmd = posix_native_host.spawned[0][1]
    run = "cli.commands._updater_stage run"
    final = "cli.commands._updater_stage final"
    assert run in cmd
    assert final in cmd
    assert cmd.index(run) < cmd.index("if cd")
    assert cmd.index(final) > cmd.index("fi;")


@pytest.mark.real_cluster_spawn
@pytest.mark.parametrize(
    "restart_only",
    [False, True],
    ids=["update", "restart-only"],
)
def test_the_native_updater_ladder_carries_per_stage_markers(
    restart_only: bool, native_host: _FakeSessionBackend
) -> None:
    """The cmd.exe ladder emits `[updater] stage=` markers between its steps
    (Task #1820 — per-host updater stage telemetry).

    `ops.updater_outcome._parse_stages` pairs the `t=` timestamps into the
    fetch/checkout/uv breakdown the rollout report shows; a ladder without the
    markers is exactly the Windows 75.9s decision the brief could not subdivide.
    A full update marks fetch/checkout/uv/restart; a restart-only bounce has no
    fetch/checkout to measure, so it marks only the restart that follows.
    """
    cluster_mod.spawn_update(restart_only=restart_only)

    cmd = native_host.spawned[0][1]
    expected = (
        ["run", "fetch", "checkout", "uv", "restart", "done", "final"]
        if not restart_only
        else ["run", "restart", "done", "final"]
    )
    for stage in expected:
        assert f"cli.commands._updater_stage {stage}" in cmd, f"missing {stage} marker"
    if not restart_only:
        # Fail-soft like the source-switch markers: a marker that cannot print
        # must not abort the update it is there to measure.
        assert f"_updater_stage {expected[0]} || ver>nul" in cmd


@pytest.mark.real_cluster_spawn
def test_spawn_update_logs_nonzero_validate_fetch(
    native_host: _FakeSessionBackend,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed best-effort fetch remains visible before validation fails closed."""
    monkeypatch.setattr(cluster_deploy, "run_bounded", _failed_fetch)

    with caplog.at_level(logging.WARNING, logger="ops.cluster_deploy"):
        cluster_mod.spawn_update()

    assert "rc=128" in caplog.text
    assert "fatal: unable to access origin" in caplog.text


@pytest.mark.real_cluster_spawn
@pytest.mark.parametrize("restart_only", [False, True])
def test_native_updater_chains_touch_and_clear_the_lease(
    restart_only: bool, native_host: _FakeSessionBackend
) -> None:
    """Both native updater shapes claim the handoff before any mutation.

    The claim is a hard gate: unlike the observational DB-lease write inside
    ``touch``, its failure must short-circuit the checkout/restart chain.  Every
    terminal arm still attempts the generation-scoped clear.
    """
    cluster_mod.spawn_update(restart_only=restart_only)

    cmd = native_host.spawned[0][1]
    touch = "cli.commands._updater_lease touch"
    assert touch in cmd
    assert "cli.commands._updater_lease clear" in cmd
    after_touch = cmd.split(touch, 1)[1]
    claim, separator, _mutations = after_touch.partition("&&")
    assert separator == "&&"
    assert "||" not in claim


@pytest.mark.real_cluster_spawn
def test_the_native_update_chain_records_the_installed_sha_after_its_sync(
    native_host: _FakeSessionBackend,
) -> None:
    """The bookmark the in-process POSIX entry writes and this chain did not, which
    is why a Windows host greeted its own rollout with SOURCE INTEGRITY VIOLATION and
    paid the guard's compensating `uv sync` every update.

    Two things are asserted about placement, and both are the point: it runs AFTER
    `uv sync` (the bookmark claims a completed install, so writing it earlier would
    make the claim false), and it is chained with `&&` so a failed sync still
    short-circuits — with the `|| ver>nul` inside the group keeping the bookmark
    itself non-fatal."""
    cluster_mod.spawn_update()

    cmd = native_host.spawned[0][1]
    sync_step = "python -m cli.commands._update_uv_sync"
    step = "(python -m cli.commands._installed_sha || ver>nul)"
    assert sync_step in cmd
    assert step in cmd
    assert cmd.index(sync_step) < cmd.index(step), "the bookmark must follow the sync"
    assert f"&& {step}" in cmd, "a failed uv sync must still short-circuit here"


@pytest.mark.real_cluster_spawn
def test_the_restart_only_native_chain_records_no_installed_sha(
    native_host: _FakeSessionBackend,
) -> None:
    """A restart-only bounce checks nothing out and syncs nothing, so it has no new
    install to claim — writing the bookmark there would assert an install that did
    not happen and mask a genuine drift on the next start."""
    cluster_mod.spawn_update(restart_only=True)

    assert "cli.commands._installed_sha" not in native_host.spawned[0][1]


@pytest.mark.real_cluster_spawn
@pytest.mark.parametrize("restart_only", [False, True])
def test_every_terminal_branch_of_the_native_ladder_states_its_own_rc(
    restart_only: bool, native_host: _FakeSessionBackend
) -> None:
    """cmd.exe cannot expand the errorlevel at the end of a command line, so the
    ladder states each arm's verdict literally instead — a clean restart, a
    preflight refusal and a failed-then-recovered restart each end in the
    `[session-exit] rc=` line `ops.updater_outcome` parses. Without all three, a
    Windows host that finished and one that died at `git fetch` read the same."""
    cluster_mod.spawn_update(restart_only=restart_only)

    cmd = native_host.spawned[0][1]
    for rc in (0, 1, RESTART_DECLINED_EXIT_CODE):
        assert uo.native_exit_line(rc) in cmd, f"the ladder never reports rc={rc}"


@pytest.mark.real_cluster_spawn
def test_the_native_abort_branch_clears_the_lease_before_it_exits_cmd(
    native_host: _FakeSessionBackend,
) -> None:
    """`exit /b` outside a batch script exits cmd.exe, taking the rest of the
    command line — including the chain's trailing lease clear — with it. So the
    abort branch has to clear the lease itself, or the cheapest failure there is
    (a `git fetch` that cannot reach origin) leaves the host claiming a live
    updater for the lease's whole TTL, which is exactly as long as Phase B is
    willing to wait for a host that is still working."""
    cluster_mod.spawn_update()

    cmd = native_host.spawned[0][1]
    abort = cmd[cmd.index("checkout/sync or tree verification FAILED") :]
    assert "cli.commands._updater_stage final" not in abort
    clear = abort.index("cli.commands._updater_lease clear")
    assert clear < abort.index("exit /b 1"), "the abort exits cmd.exe before clearing the lease"


@pytest.mark.real_cluster_spawn
def test_the_native_ladder_opens_and_closes_the_source_switch_window(
    native_host: _FakeSessionBackend,
) -> None:
    """The checkout replaces the tree file by file while the old daemons are
    still running; the ladder must open the source-switch window BEFORE the
    checkout and close it on every exit path (the chain tail AND the abort arm,
    which exits cmd.exe before the tail — the same shape the lease clear pins).
    Fail-soft (`|| ver>nul`) so the first rollout that ships the marker (whose
    pre-checkout tree predates the module) cannot break on ModuleNotFoundError."""
    cluster_mod.spawn_update()

    cmd = native_host.spawned[0][1]
    on = cmd.index("cli.commands._source_switch_marker on")
    checkout = cmd.index("git checkout --force")
    assert on < checkout, "the marker must open before the checkout"
    abort = cmd[cmd.index("checkout/sync or tree verification FAILED") :]
    assert "cli.commands._source_switch_marker off" in abort, "abort arm must close the window"
    assert "|| ver>nul" in cmd, "the marker steps must be fail-soft"


@pytest.mark.real_cluster_spawn
def test_restart_only_ladder_carries_no_source_switch_steps(
    native_host: _FakeSessionBackend,
) -> None:
    """A restart-only bounce does no checkout — the tree is never being
    switched, so the ladder must not mark a window (or hold respawns back for
    nothing)."""
    cluster_mod.spawn_update(restart_only=True)

    cmd = native_host.spawned[0][1]
    assert "cli.commands._source_switch_marker" not in cmd


@pytest.mark.real_cluster_spawn
@pytest.mark.parametrize("restart_only", [False, True])
def test_posix_updater_runs_the_in_process_entry(
    restart_only: bool, posix_native_host: _FakeSessionBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The POSIX chain IS the in-process self-update (R1-6 execution-shape
    convergence): `python -m cli.commands._update_agent_runner` with the op
    payload as flags, no hand-built checkout/sync/restart ladder. The lease
    lives inside the entry (touch on entry, clear in a finally), so the shell
    carries neither `_updater_lease` step; `[session-exit] rc=$rc` still reports
    the entry's verdict to `ops.updater_outcome`."""
    monkeypatch.setattr("shared.migrations.validate_migrations_at_ref", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
    cluster_mod.spawn_update(restart_only=restart_only)

    cmd = posix_native_host.spawned[0][1]
    assert "python -m cli.commands._update_agent_runner" in cmd
    assert "--mode smooth" in cmd  # the drain policy is always spelled out
    assert 'echo "[session-exit] rc=$rc"' in cmd
    assert "cli.commands._updater_lease" not in cmd  # lease moved into the entry
    if restart_only:
        assert "--restart-only" in cmd
        assert "--target-sha" not in cmd
    else:
        assert "--restart-only" not in cmd


@pytest.mark.real_cluster_spawn
def test_spawn_update_rolls_back_the_pause_when_the_native_spawn_fails(
    native_host: _FakeSessionBackend, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed spawn must return the posture to idle — a host left paused with
    no updater in flight strands its agents."""
    calls: list[str] = []
    monkeypatch.setattr("shared.host_deploy_state.set_posture", calls.append)
    monkeypatch.setattr(native_host, "new_session", lambda *_a, **_k: False)  # pyright: ignore[reportUnknownArgumentType]

    with pytest.raises(RuntimeError):
        cluster_mod.spawn_update(restart_only=True)

    assert calls and calls[-1] == "idle"


@pytest.mark.real_cluster_spawn
@pytest.mark.parametrize("restart_only", [False, True], ids=["update", "restart-only"])
def test_posix_host_spawns_the_orchestration_session_on_the_session_backend(
    posix_native_host: _FakeSessionBackend,
    restart_only: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S7: the POSIX orchestration session lands on the SAME session backend as
    services — `get_backend().new_session(session, shell_cmd, ...)` — with the
    shell command intact: the tee pipeline, the `[session-exit]` verdict and
    the venv activation all survive the move. No raw-binary argv exists anymore."""
    monkeypatch.setattr("shared.migrations.validate_migrations_at_ref", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
    cluster_mod.spawn_update(restart_only=restart_only)

    assert [s[0] for s in posix_native_host.spawned] == ["ava-test-updater"]
    _name, cmd, cwd = posix_native_host.spawned[0]
    assert cwd == cluster_mod._REPO_ROOT
    assert "tee -a" in cmd
    assert 'echo "[session-exit] rc=$rc"' in cmd
    assert "python -m cli.commands._update_agent_runner" in cmd
    assert "export AVA_CLI_LOG_NAME=updater" in cmd


@pytest.mark.real_cluster_spawn
@pytest.mark.parametrize("restart_only", [False, True], ids=["update", "restart-only"])
def test_native_updater_exports_cli_log_name(
    native_host: _FakeSessionBackend, restart_only: bool
) -> None:
    """Every Windows updater child inherits the sink name for converge errors."""
    cluster_mod.spawn_update(restart_only=restart_only)

    cmd = native_host.spawned[0][1]
    assert "set AVA_CLI_LOG_NAME=updater && " in cmd


@pytest.mark.real_cluster_spawn
def test_backend_decline_raises_orchestration_spawn_failed(
    posix_native_host: _FakeSessionBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the session backend declines to start the updater, the diagnosis is
    OrchestrationSpawnFailed (the gateway's 503) and the pause is rolled back —
    the same fail-fast the old missing-backend path gave, minus the legacy
    dependency."""
    calls: list[str] = []
    monkeypatch.setattr("shared.host_deploy_state.set_posture", calls.append)
    original_new_session = posix_native_host.new_session

    def _decline_only_updater(
        name: str,
        cmd: str,
        cwd: Path,
        *,
        env: dict[str, str],
        login_shell: bool = True,
        exec_cmd: bool = True,
    ) -> bool:
        if name == "ava-test-updater":
            return False
        return original_new_session(
            name,
            cmd,
            cwd,
            env=env,
            login_shell=login_shell,
            exec_cmd=exec_cmd,
        )

    monkeypatch.setattr(posix_native_host, "new_session", _decline_only_updater)

    with pytest.raises(cluster_mod.OrchestrationSpawnFailed):
        cluster_mod.spawn_update(restart_only=True)
    assert calls and calls[-1] == "idle"


class TestNativeArg:
    """`_native_arg` is the only quoting cmd.exe gets — `shlex.quote` is wrong there."""

    def test_wraps_in_double_quotes(self) -> None:
        assert cluster_mod._native_arg("abc1234") == '"abc1234"'

    def test_shell_operators_are_literal_inside_the_quotes(self) -> None:
        # `&`, `|`, `<`, `>`, `^` lose their meaning inside a cmd.exe quoted run,
        # so they need no escaping — only wrapping.
        assert cluster_mod._native_arg("a&b|c>d") == '"a&b|c>d"'

    @pytest.mark.parametrize("bad", ['a"b', "a\nb", "a\rb"])
    def test_refuses_what_cannot_be_represented(self, bad: str) -> None:
        with pytest.raises(ValueError, match=r"cmd\.exe"):
            cluster_mod._native_arg(bad)


def test_the_facade_imports_every_submodule_eagerly() -> None:
    """`ops/cluster.py` must import all four submodules at module level.

    `tests/conftest.py`'s `_guard_cluster_spawn` stubs the spawn entry points via
    `_stub_everywhere`, which finds aliases by object identity but only across
    modules the run has already imported ("Nothing is imported to find them"). The
    facade's eager re-exports are what make that scan reach the definition sites.
    Converted to lazy / `__getattr__` imports it would reach only whichever
    submodules an earlier test happened to load, so the guard would cover some of
    them and a test could spawn a real `ava cluster update` with nothing failing to say so.

    Checked statically, on the source: asserting it by re-importing at runtime would
    have to evict the modules from `sys.modules` first, and every already-imported
    consumer (`gateway/routers/*`, `ops/ops_cluster.py`) holds references to the
    function objects those modules defined — swapping them mid-session is how a
    guard test becomes the thing that breaks unrelated tests.
    """
    import ast
    from pathlib import Path

    src = Path(cluster_mod.__file__).read_text(encoding="utf-8")
    top_level_imports = {
        node.module
        for node in ast.parse(src).body
        if isinstance(node, ast.ImportFrom) and node.module
    }
    expected = {
        "ops.cluster_session",
        "ops.cluster_pause",
        "ops.cluster_status",
        "ops.cluster_deploy",
    }
    assert expected <= top_level_imports, (
        f"ops/cluster.py must import these at module level, missing: "
        f"{sorted(expected - top_level_imports)}"
    )

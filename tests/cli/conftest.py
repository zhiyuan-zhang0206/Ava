"""Shared fixtures for the CLI tests.

Orchestration tests record the local pause/resume seam while updating the
actual host posture. Without that posture effect, a fake recovery would leave
later gateway requests behind a stale 503 gate. Production service and OS job
boundaries remain guarded by the root fixtures and the local gate stubs.
"""

from __future__ import annotations

import pathlib
import subprocess
from collections.abc import Callable, Generator, Iterator
from contextlib import AbstractContextManager, contextmanager

import pytest

from shared import disabled_services as ds
from shared.config import settings


@pytest.fixture
def stub_deploy_lease_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pair tests' fake successful acquire with the exact lease it implies."""
    from datetime import UTC, datetime

    from cli.commands import update as _up
    from shared.cluster_lock import DeployLease

    monkeypatch.setattr(
        _up,
        "read_update_lease",
        lambda: DeployLease(
            holder=_up.self_holder(),
            held_for_s=0,
            expires_in_s=60,
            note=None,
            kind="rollout",
            acquired_at=datetime(2026, 8, 25, tzinfo=UTC),
        ),
    )


@pytest.fixture(autouse=True)
def _gate_probe_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the gate observation off this box's real entry port and real launchd.

    `ava status` and the health probe now observe the gate (`probe_gate`), which
    dials `127.0.0.1:<entry port>` and shells out to `launchctl`. On a dev box that
    entry port belongs to the operator's LIVE prod gate and that label is their real
    launchd job, so both seams are stubbed for the whole directory rather than left
    to each test to remember — the same reason `AVA_OS_JOBS_ENABLED=false` exists.

    The default answers are "nothing on the port, no such job", which is what a
    hermetic host looks like. Tests that assert a particular gate state (including
    `_ensure_launchd`'s own) install their own `_launchctl` on top."""
    import cli.commands._converge_gate as cg
    import cli.commands._gate_systemd as gs

    monkeypatch.setattr(
        gs,
        "_systemctl",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 1, "LoadState=not-found\nActiveState=inactive\n", ""
        ),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(gs, "unit_path", lambda home: home / "test-systemd" / gs.unit_name(home))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(cg, "_entry_answers", lambda _port: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        cg,
        "_launchctl",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "no such process"),  # pyright: ignore[reportUnknownArgumentType]
    )


@pytest.fixture(autouse=True)
def local_unpauses(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Record orchestration resume while restoring its actual HTTP posture.

    A stub that records without clearing posture leaks 503 responses into
    unrelated TestClient requests later in the pytest process.
    """
    calls: list[bool] = []

    def _unpause() -> None:
        # The real unpause writes the R1 host_deploy_state posture row
        # (ops/cluster_pause.py); the stub stands in for that function, so its
        # observable effects must match — a test that asserts the pause
        # lifecycle through the orchestration relies on it, and a leftover
        # `paused` row would 503 every gateway test in this session
        # (host_deploy_state IS in the per-test TRUNCATE list — tests/conftest.py;
        # the R1 singleton deployment_state is not, and self-cleans via
        # acquire/release pairs + test_cluster_lock's _free_lock).
        from shared.host_deploy_state import set_posture

        set_posture("idle")
        calls.append(True)

    monkeypatch.setattr("ops.cluster.unpause_local_cluster", _unpause)
    return calls


@pytest.fixture(autouse=True)
def _prepare_checks_are_explicit_in_orchestration_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep legacy phase tests focused on their named commit-stage boundary.

    Prepare has its own gate suite. Tests that exercise Phase A, the local leg,
    readiness, or Phase B use synthetic commit IDs and therefore supply a local
    prepared recovery tuple rather than constructing a real detached worktree.
    A test of prepare itself overrides these seams explicitly.
    """
    from cli.commands import update as _up

    monkeypatch.setattr(_up, "dry_run_checks", lambda *_args, **_kw: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "estimate_maintenance_window", lambda: 85.0)
    monkeypatch.setattr(
        _up,
        "_snapshot_known_good",
        lambda *, pull, target_sha: ("prepared-sha", set(), None) if pull else None,  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_up, "_finalize_commit_telemetry", lambda _telemetry: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_spawn_async_offsite_upload", lambda _repo, _dump: None)  # pyright: ignore[reportUnknownArgumentType]


@pytest.fixture
def _installed_machine_identity(unit_home: pathlib.Path) -> Iterator[None]:
    """Give this test's unit home a machine identity, as a real install has.

    `unit_home` is deliberately a bare directory — several tests model a home
    that has never been through `ava start` and assert exactly that (see
    `test_start_arg_writes_to_file_for_persistence`, whose premise is "files
    also do not exist"). So the identity cannot be pre-written there.

    But an install that lands content into the CLUSTER registry records
    `local:<machine>` provenance for a local-path source, which needs a machine
    name — and a home doing `ava skill install` is by definition an installed
    unit, not a virgin one. Modules exercising the install paths opt in with
    `pytestmark = pytest.mark.usefixtures("_installed_machine_identity")`.
    """
    from shared.machine import reset_identity

    (unit_home / "machine_name").write_text("unit-test-machine", encoding="utf-8")
    reset_identity()
    yield
    reset_identity()


@pytest.fixture
def as_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[pathlib.Path], AbstractContextManager[pathlib.Path]]:
    """Run a block as the machine whose `$AVA_HOME` is the given directory.

    A "machine" in these tests IS its `$AVA_HOME`: `skills_dir()`, the
    `installed.json` install registry and `machine_name` all hang off it, so
    pointing `settings.general.ava_home` at a second directory gives a genuinely
    distinct machine to every path under test while the Postgres URL is
    untouched. Two homes, one PG.

    `reset_identity()` on BOTH edges is the load-bearing part, not the home
    swap: `machine_name()` caches, and a stale cache would let the second home
    claim to be the first — which would make a cross-machine test pass for the
    wrong reason, since rows record `local:<machine>`.

    Shared rather than copied because it is exactly the kind of helper whose
    subtle half (the cache reset) gets dropped in the copy.
    """
    from shared.machine import reset_identity

    @contextmanager
    def _enter(home: pathlib.Path) -> Generator[pathlib.Path]:
        home.mkdir(parents=True, exist_ok=True)
        (home / "machine_name").write_text(home.name, encoding="utf-8")
        with monkeypatch.context() as m:
            m.setattr(settings.general, "ava_home", home)
            reset_identity()
            try:
                yield home
            finally:
                reset_identity()

    return _enter


@pytest.fixture(autouse=True)
def _isolate_disabled_services_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Every CLI test's durable `--disable-service` marker lives in a per-test tmp
    file, never in the worker's shared session home.

    An operator `ava start --disable-service X` persists X to
    `$AVA_HOME/disabled_services` (`persist_services=True` — the real, intended
    behavior the start tests exercise). The suite's session home is shared by
    every test in a worker, so an un-isolated marker outlives the test that wrote
    it: a `cmd_start(disabled_services=("restarter",))` left "restarter durably
    disabled" behind, and a later `unpause_local_cluster` test in the same worker
    early-returned — neither respawning the restarter nor raising (CI #1172/#1173
    shard-5 flake, task #2177). Same redirection `tests/shared/test_disabled_services.py`
    uses: the marker is per-unit durable state, so each test gets a fresh one.
    """
    monkeypatch.setattr(ds, "disabled_services_file", lambda: tmp_path / "disabled_services")


@pytest.fixture(autouse=True)
def _isolate_local_pause_journal(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed stop intentionally retains its hold; it must not hold the next test."""
    from shared import pause_owner

    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause-owner.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause-owner.lock")


@pytest.fixture(autouse=True)
def local_pauses(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Orchestration tests stub the native daemon boundary explicitly.

    Kernel integration tests call ops.agent_pause or the public command kernel
    directly; a phase-ordering test must not dial this host's real agent-host.
    """
    calls: list[bool] = []
    monkeypatch.setattr("ops.cluster.pause_local_cluster", lambda: calls.append(True))
    return calls

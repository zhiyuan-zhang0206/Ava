"""Shared fixtures for the CLI tests.

Orchestration tests record the local pause/resume seam while updating the
actual host posture. Without that posture effect, a fake recovery would leave
later gateway requests behind a stale 503 gate. Production service and OS job
boundaries remain guarded by the root fixtures.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable, Generator, Iterator
from contextlib import AbstractContextManager, contextmanager

import pytest

from shared import service_selection as ds
from shared.config import settings


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
    monkeypatch.setattr(ds, "selection_path", lambda: tmp_path / "service-selection.json")


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
    monkeypatch.setattr("ops.cluster_pause.pause_local_cluster", lambda: calls.append(True))
    return calls

"""Shared fixtures for the CLI tests (registered by `tests/fixtures/path_scopes.py`).

Orchestration tests record the local hold/resume seam while updating the
actual host posture. Without that posture effect, a fake recovery would leave
later gateway requests behind a stale 503 gate. Production service and OS job
boundaries remain guarded by the root fixtures.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable, Generator, Iterator
from contextlib import AbstractContextManager, contextmanager

import pytest

from base.deploy.lifecycle import service_selection as ds
from tests.path_scoped.pty_reaper import pty_reaper as pty_reaper


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
    from base.cluster.machine import reset_identity, set_identity

    set_identity(name="unit-test-machine")
    yield
    reset_identity()


@pytest.fixture
def as_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[pathlib.Path], AbstractContextManager[pathlib.Path]]:
    """Run a block as the machine whose `$AVA_HOME` is the given directory.

    A "machine" in these tests IS its `$AVA_HOME`: `skills_dir()`, the
    `installed.json` install registry and `machine_name` all hang off it, so
    pointing `AVA_HOME` at a second directory gives a genuinely
    distinct machine to every path under test while the Postgres URL is
    untouched. Two homes, one PG.

    `reset_identity()` on BOTH edges is the load-bearing part, not the home
    swap: `machine_name()` caches, and a stale cache would let the second home
    claim to be the first — which would make a cross-machine test pass for the
    wrong reason, since rows record `local:<machine>`.

    Shared rather than copied because it is exactly the kind of helper whose
    subtle half (the cache reset) gets dropped in the copy.
    """
    from base.cluster.machine import reset_identity, set_identity

    @contextmanager
    def _enter(home: pathlib.Path) -> Generator[pathlib.Path]:
        home.mkdir(parents=True, exist_ok=True)
        with monkeypatch.context() as m:
            m.setenv("AVA_HOME", str(home))
            reset_identity()
            set_identity(name=home.name)
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
    shard-5 flake, task #2177). Same redirection `tests/base/test_disabled_services.py`
    uses: the marker is per-unit durable state, so each test gets a fresh one.
    """
    monkeypatch.setattr(ds, "selection_path", lambda: tmp_path / "service-selection.json")


@pytest.fixture(autouse=True)
def _isolate_local_pause_journal(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed stop intentionally retains its hold; it must not hold the next test."""
    from base.deploy.maintenance import pause_owner

    monkeypatch.setattr(pause_owner, "state_path", lambda: tmp_path / "pause-owner.json")
    monkeypatch.setattr(pause_owner, "lock_path", lambda: tmp_path / "pause-owner.lock")


@pytest.fixture(autouse=True)
def cli_log_sinks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """In-process dispatch opens no real loguru sinks; the names asked for are recorded.

    `ava start`, `restart`, `maintenance start` and `lgtm on|off` open the
    service sink set (`cli.main._init_cli_logging`). A real one here would
    outlive its test: a stderr sink bound to that test's captured stream, a
    file sink in the session home and the process-wide init guard latched.
    """
    opened: list[str] = []

    def record(*, name: str) -> None:
        opened.append(name)

    monkeypatch.setattr("base.log.init_cli_process", record)
    return opened

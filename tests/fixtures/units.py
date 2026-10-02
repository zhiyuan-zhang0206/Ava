"""Domain fixtures for the two-unit cluster model and per-test unit homes.

The session default is one agent-runner unit under one tmpfs home (see
`tests.fixtures.env_bootstrap`). These fixtures let a test state which unit it
exercises instead of patching the identity at every import site: a gateway or a
runner (`gateway_unit`, `runner_unit`, `set_machine_identity`), a fresh per-test
home (`unit_home`, `workspace`), a private write-generation ledger
(`seed_write_generation`, `served_gateway_home`), a local-root observation double
(`serving_root`), and `spawn_agent`, which allocates a real agent row on this
unit. They are additive: none changes the session default for tests that do not
take them.
"""

import contextlib
from collections.abc import Callable, Generator, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

import ava
from base.deploy.lifecycle.start_serving import RootBirth

# ── Two-unit fixtures: model "a gateway unit" and "a runner unit" explicitly ──
#
# Background: this session module-loads with machine_role='agent-runner' + one
# AVA_HOME, because most SDK tests need spawn_agent to take the local-spawn path
# (the gateway invariant rejects local spawn). gateway tests have
# historically monkeypatched machine_role back per-module (e.g. patching both
# `status_router.machine_role` and `cluster_mod.machine_role`). These fixtures
# centralize the role switch at its source (the identity holder) so a test
# states which unit it exercises instead of patching every import site.
#
# They are additive: the session default (agent-runner) is unchanged, so existing
# tests keep working. New / migrated tests opt in by taking `gateway_unit` /
# `runner_unit`.


@contextlib.contextmanager
def _machine_identity(*, role: str, name: str | None = None) -> Generator[None]:
    """Switch this process's resolved machine identity, restoring on exit.

    Injects via base.cluster.machine.set_identity so every `from base.cluster.machine import
    machine_role` / `machine_name` call site sees the new value without
    per-module patching. `name=None` leaves machine_name as-is — no injection; it
    resolves lazily from settings if not yet cached, otherwise
    returns the already-cached value. The finally block resets the holder, so the
    session default is restored — no per-field save/restore is needed because the
    holder re-resolves lazily after reset.
    """
    from base.cluster.machine import reset_identity, set_identity

    if name is None:
        set_identity(role=role)  # pyright: ignore[reportArgumentType]  # str passthrough to MachineRole literal
    else:
        set_identity(role=role, name=name)  # pyright: ignore[reportArgumentType]
    try:
        yield
    finally:
        reset_identity()


@pytest.fixture
def set_machine_identity() -> Iterator[object]:
    """Factory: switch machine role (and optionally name) at the source via
    base.cluster.machine.set_identity.

        def test_x(set_machine_identity, db_conn):
            set_machine_identity(role="gateway", name="cloud-test")

    May be called more than once within a test to flip roles; the last call
    wins, and the holder is reset at teardown (restoring the session default).
    """
    from base.cluster.machine import reset_identity, set_identity

    def _set(role: str, name: str | None = None) -> None:
        if name is None:
            set_identity(role=role)  # pyright: ignore[reportArgumentType]
        else:
            set_identity(role=role, name=name)  # pyright: ignore[reportArgumentType]

    try:
        yield _set
    finally:
        reset_identity()


@pytest.fixture
def gateway_unit(db_conn: psycopg.Connection) -> Iterator[TestClient]:
    """The gateway unit (gateway): owns the test DB/config, runs the
    in-process gateway app, role='gateway'.

    Yields a FastAPI TestClient bound to the gateway app. Use for tests that
    exercise gateway endpoints / invariants without hand-patching
    machine_role at each import site. db_conn gives the per-test TRUNCATE. For a
    specific machine_name, also take `set_machine_identity` and call it.

    The cluster-secret auth middleware is disabled by the autouse `_clean_state`
    (auth_middleware_enabled=false) so in-process endpoint tests don't need to
    carry auth headers. Tests that specifically exercise the auth middleware
    re-enable it via monkeypatch.
    """
    from gateway.app import app as _app

    _ = db_conn  # per-test truncate side effect
    with (
        _machine_identity(role="gateway"),
        TestClient(_app, base_url="http://test-gateway") as client,
    ):
        yield client


@pytest.fixture
def sdk_via_gateway(gateway_unit: TestClient) -> Iterator[TestClient]:
    """The gateway unit with the SDK's gateway client pointed at it: `ava.agents.*` calls land
    in the in-process app. Undone at teardown."""
    from ava.gateway_client.transport import use_client

    with use_client(gateway_unit):
        yield gateway_unit


@pytest.fixture
def runner_unit(db_conn: psycopg.Connection) -> Iterator[None]:
    """The agent-runner unit (agent-runner): role='agent-runner', holds a
    gateway_url pointing at the gateway, no local gateway/docker.

    This is the SDK side — spawn_agent takes the local-spawn path here. Matches
    the session default role, but as an explicit opt-in so a test declares it is
    the runner unit rather than relying on the global default.
    """
    _ = db_conn  # per-test truncate side effect
    with _machine_identity(role="agent-runner"):
        yield


@pytest.fixture
def unit_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point this unit's home at a fresh per-test tmp dir and reset machine
    identity so a test that writes machine_name/role/description files (or any
    $AVA_HOME-derived path) is isolated from the session home.

    The dir it yields is BARE — no `machine_name`, no `role`, no `.env`. That is
    not an oversight to fix: this fixture legitimately models two different
    homes, and an empty dir is the only state both of them start from.

    * A **virgin home** — never installed, never through `ava start`. Tests here
      assert the ABSENCE of the identity files, e.g.
      `test_start_arg_writes_to_file_for_persistence`, whose premise is "all env
      empty, files also do not exist".
    * An **installed unit** — a home some earlier step gave an identity. The CLI
      tests that run `ava skill install` need this one: a local-path install
      records `local:<machine>` provenance in the cluster registry, and
      `machine_name()` raises when the file is missing.

    **Do not pre-write an identity here to make the second kind pass.** It looks
    free — the installed-unit tests go green and nothing else obviously cares —
    but it silently converts every `unit_home` in the repo to the installed
    meaning. The only tests that can object are the few whose whole premise is
    the absence, and they object only if their premise was written down; the
    rest stay green while quietly testing a different world.

    The opt-in for the installed meaning is `_installed_machine_identity` in
    `tests/path_scoped/cli_tests.py` — a module takes it with one line:
    `pytestmark = pytest.mark.usefixtures("_installed_machine_identity")`.
    """
    from base.cluster.machine import reset_identity

    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    reset_identity()
    yield tmp_path
    reset_identity()


@pytest.fixture
def default_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake OS user whose `~/.ava` is this test's home, and so the default home.

    OS jobs belong to the default home only (`base.host.system.cron.owns_os_jobs`),
    so a test of a registrar or unregistrar needs this, not `unit_home`. `HOME`
    points into the tmp dir, which also keeps `~/Library/LaunchAgents` and the
    crontab lock off the operator's real ones; yields the user's home, whose
    `.ava` is the default home."""
    user_home = tmp_path / "user"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("AVA_HOME", str(user_home / ".ava"))
    return user_home


def skip_authority_pass(_home: Path) -> None:
    """Stand-in for `dotenv_boot._enforce_cluster_env_authority` where a test
    exercises the rest of the boot and must not have its env rewritten."""


def use_env_files(
    monkeypatch: pytest.MonkeyPatch, env_file: Path, mirror_file: Path | None = None
) -> Path:
    """Name, through `AVA_HOME`, a home whose `.env` (and `mirror.env`) carry these
    files' content: for a test that hand-builds the files a boot pass reads.

    The home is a sibling directory of `env_file`; the variable is the only thing
    the boot reads, so nothing else needs patching."""
    home = env_file.parent / f"{env_file.stem}-home"
    home.mkdir(exist_ok=True)
    (home / ".env").write_text(env_file.read_text())
    if mirror_file is not None and mirror_file.exists():
        (home / "mirror.env").write_text(mirror_file.read_text())
    monkeypatch.setenv("AVA_HOME", str(home))
    return home


@pytest.fixture
def workspace(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This test's agent workspace dir (the relative-path base of ava.files /
    ava.shell.run / ava.understand path mode), under the per-test unit home.

    Pins the agent id explicitly via monkeypatch instead of relying on the
    session-global `ava.agent_identity._agent_id = 1` staying unmutated across test
    ordering (a leak through that global is exactly what the `_isolated_agent`
    monkeypatch fix in tests/path_scoped/ava_tests.py guards against). The dir is NOT
    pre-created — `workspace_dir` mkdirs on first resolution, and several
    tests assert exactly that; pre-create with `.mkdir(parents=True)` when a
    test seeds files into it.
    """
    monkeypatch.setattr(ava.agent_identity, "_agent_id", 1)
    return unit_home / "workspaces" / "1"


# Test agents are durable rows; only a real host process executes their turns.


def spawn_agent(
    *,
    spawner: str = "user",
    config: dict[str, object] | None = None,
    **kw: Any,
) -> int:
    """Allocate a real agent row and publish its normal host-dispatch wake."""
    from base.cluster.machine import machine_name
    from base.db import publish_inbound_wake
    from ops.agents.spawn import create_agent_row

    agent_id, _, _prompt_id, _attempt_id = create_agent_row(
        spawner=spawner, machine=machine_name(), config=config, **kw
    )
    publish_inbound_wake(agent_id, "0")
    return agent_id


@pytest.fixture
def serving_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> RootBirth:
    """Explicit local-root observation double; excludes native/root proof.

    Maintenance/readiness unit tests retain the actual marker and locking code.
    Native IPC and loaded-origin contracts have independent real socket tests.
    """
    from base.deploy.lifecycle import start_serving
    from base.deploy.release.runtime_interpreter import LoadedRuntimeIdentity
    from base.native_process.evidence import ExpectedProcess

    runtime = LoadedRuntimeIdentity(
        kind="source",
        code_root=str(tmp_path),
        interpreter=str(tmp_path / "python"),
        prefix=str(tmp_path / "venv"),
        cwd=str(tmp_path),
        source_digest="a" * 64,
    )
    birth = start_serving.RootBirth(
        home=str(tmp_path),
        process=ExpectedProcess(pid=4321, create_time=123.0, starttime=456),
        launch_digest="b" * 64,
        runtime=runtime,
    )
    monkeypatch.setattr(start_serving, "_observe_root", lambda: birth)
    return birth


@pytest.fixture
def seed_write_generation() -> Callable[[Path], Any]:
    """Record an active write generation in a home's private ledger, no database.

    For code that only READS the ledger (launch delivery, bootstrap projection,
    operator consumption): the catalog side is proven on real PostgreSQL in
    tests/lifecycle/db_authority/. Returns the generation's secret record.
    """
    from base.cluster.authority import (
        GATEWAY_GROUP,
        RUNNER_GROUP,
        BirthAuthority,
        Groups,
        VerifiedGeneration,
        activate,
        create_ledger,
        read_secret,
    )
    from base.cluster.authority.ledger import begin_mint

    def seed(home: Path) -> Any:
        home = home.resolve()
        birth = BirthAuthority()
        groups = Groups(gateway=GATEWAY_GROUP, runner=RUNNER_GROUP)
        create_ledger(home, owner="ava", groups=groups, authority=birth)
        pending = begin_mint(
            home, birth, encrypt=lambda name, _pw: f"SCRAM-SHA-256$4096:c2VlZA==${name}"
        )
        activate(
            home,
            birth,
            VerifiedGeneration(pending.number, pending.credential_digest, pending.roles),
        )
        return read_secret(home, pending)

    return seed


@pytest.fixture
def served_gateway_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, seed_write_generation: Callable[[Path], Any]
) -> Any:
    """The suite's gateway `.env` served from a private home that keeps an active
    write generation: bootstrap's local runner projection reads that ledger.
    Returns the generation's secret record."""
    import shutil

    from base.host.env import runtime_config as rt

    shutil.copy(rt.env_file_path(), tmp_path / ".env")
    monkeypatch.setattr(rt, "_ava_home", lambda: tmp_path)
    return seed_write_generation(tmp_path)

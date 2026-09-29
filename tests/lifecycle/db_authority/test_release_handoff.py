"""A release's database authority across its image boundaries, A -> B -> A.

Real PostgreSQL 17, PgBouncer, Redis and the home's write-generation ledger
(`test_single_box.born`); nothing that dials is mocked. Each direction crosses
three processes, run here one after another, each in exactly the environment
the real one receives:

1. the selected image's `ava cluster update --prepared` (the handoff), whose
   exec into the executor image is captured instead of performed;
2. the executor image's `submit` entry in that captured environment: its boot
   pass, then the real submission preflight dialing through the pooler;
3. the finite executor in the environment its Linux launch plan records: its
   boot pass, `adopt_executor_authority`, then the coordinator through the
   fence, the selection and the next write generation.

Which image a process runs is the one input the admission check reads (the
code root and interpreter prefix `require_admitted_runtime` compares); it is
the only simulated fact. The native adapter is a stand-in around the real
Linux launch plan and `systemd-run` command: no systemd runs. The application
root is `test_fleet_of_one.RootlessGateway`'s stand-in.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

import pytest
from pydantic import JsonValue

import cli.release_handoff.__main__ as entry
from cli.commands.data_plane import pgbouncer as pooler
from cli.main import _normalize_process_profile
from cli.release_fleet.coordinator import Coordinator
from cli.release_fleet.gateway import read_state
from cli.release_fleet.policy import FleetPolicy
from cli.release_fleet.publication import FleetRelease
from cli.release_fleet.request import FleetRequest, fleet_release, sql_inventory_digest
from cli.release_fleet.units import RemoteUnits
from cli.release_handoff import handoff
from cli.release_transition import launcher_linux as linux
from cli.release_transition import native
from cli.release_transition.authority import adopt_executor_authority
from cli.release_transition.journal import exclusive, read_operation
from cli.release_transition.request import ReleaseRef
from cli.start_identity import mark_phase
from shared import cluster
from shared.cluster import authority
from shared.cluster.authority import delivery
from shared.config import settings
from shared.db import connections
from shared.deploy.release.runtime_release import VerifiedRelease, current_pointer
from shared.deploy.release.start_inputs import configuration_digest
from shared.host.env import dotenv_boot
from shared.log import logger
from tests.lifecycle.db_authority.test_fleet_of_one import (
    _AGENT,
    _MACHINE,
    RootlessGateway,
    _live,
    _point,
)
from tests.lifecycle.db_authority.test_single_box import Born
from tests.lifecycle.db_authority.test_single_box import born as born
from tests.lifecycle.db_authority.test_single_box import configured as configured
from tests.lifecycle.handoff.conftest import build_image

pytestmark = [
    pytest.mark.skipif(
        not (Path(pooler.pgbouncer_bin()).exists() or shutil.which(pooler.pgbouncer_bin())),
        reason="pgbouncer not installed (brew/apt)",
    ),
    pytest.mark.skipif(os.name == "nt", reason="the handoff execs; Windows has no exec"),
]

_SITE = "venv/lib/python3.12/site-packages"
# Both images carry the same paired migration SQL: a same-schema release.
_MIGRATIONS = {
    f"{_SITE}/migrations/20260101T000000_example.sql": b"SELECT 1;\n",
    f"{_SITE}/migrations/20260101T000000_example.down.sql": b"SELECT 1;\n",
}
_FINITE_JOB_ENVIRONMENT = {"HOME", "AVA_HOME", "AVA_CLUSTER_REGISTRY", "PATH"}


class _Exec(BaseException):
    """The handoff's exec, captured: the process image would be replaced here."""


class ImageGateway(RootlessGateway):
    """The stand-in root over real images: releases are named by their SQL."""

    def release_of(self, reference: ReleaseRef) -> FleetRelease:
        return fleet_release(reference, sql_inventory_digest(reference.verify(self.home)))


@dataclass
class NativeStandIn:
    """The native adapter without systemd: the real Linux launch plan (the
    finite job's whole environment) and `systemd-run` command, never run."""

    commands: list[list[str]] = field(default_factory=list[list[str]])

    def plan_launch(self, operation: Path, runtime: VerifiedRelease) -> dict[str, JsonValue]:
        return linux.plan_launch(operation, runtime)

    def launch(self, record: dict[str, JsonValue]) -> linux.LinuxJob:
        planned = linux.LinuxLaunch.model_validate(record)
        self.commands.append(linux._launch_command(planned))
        return self._job(planned, sub="running")

    def retire_current(self, record: dict[str, JsonValue]) -> linux.LinuxJob:
        return self._job(linux.LinuxLaunch.model_validate(record), sub="exited")

    @staticmethod
    def _job(planned: linux.LinuxLaunch, *, sub: str) -> linux.LinuxJob:
        return linux.LinuxJob(
            unit=planned.unit,
            boot_id=planned.boot_id,
            invocation_id="a" * 32,
            cgroup=planned.cgroup,
            owner=None,
            active="active",
            sub=sub,
            result="success",
            exit_code=0,
            exit_status=0,
        )


@dataclass
class Cycle:
    born: Born
    registry: Path
    a: ReleaseRef
    b: ReleaseRef
    native: NativeStandIn
    # The image the simulated process runs: what its admission check reads.
    running: ReleaseRef | None = None
    # Everything a process exposed: exec argv, native commands, output, logs.
    exposed: list[str] = field(default_factory=list[str])

    @property
    def home(self) -> Path:
        return self.born.home

    def site(self, image: ReleaseRef) -> Path:
        return self.home / "releases" / image.artifact_digest / _SITE

    def request(self, previous: ReleaseRef, candidate: ReleaseRef) -> FleetRequest:
        """The captured request `ava cluster release request` writes for this home."""
        return FleetRequest(
            id=uuid4(),
            home=str(self.home),
            registry=str(self.registry),
            created_at=datetime.now(UTC),
            machine=_MACHINE,
            previous=previous,
            candidate=candidate,
            executor=candidate,
            configuration_digest=configuration_digest(self.home),
            policy=FleetPolicy(watch_s=1, min_affected=1),
        )


@contextmanager
def _process(environment: Mapping[str, str]) -> Generator[None]:
    """Run the body with exactly `environment` as the process environment."""
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(environment)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@pytest.fixture
def cycle(born: Born, monkeypatch: pytest.MonkeyPatch) -> Iterator[Cycle]:
    """The born home as the cluster's one registered unit, selecting image A."""
    import shared.cluster.machine

    home = born.home
    registry = Path(cluster.registry_path())
    cluster.save_record_locked(born.record, path=registry)
    mark_phase(home, "provisioned")
    mark_phase(home, "ready")
    monkeypatch.setattr(shared.cluster.machine, "machine_name", lambda: _MACHINE)
    monkeypatch.setattr(shared.cluster.machine, "machine_role", lambda: frozenset({"gateway"}))
    with born.admin() as conn:
        conn.execute(
            "INSERT INTO machines (name, role) VALUES (%s, '{gateway,agent-runner}')", (_MACHINE,)
        )
        conn.execute(
            "INSERT INTO machine_units (machine_name, home, serve_gateway, serve_agent_runner)"
            " VALUES (%s, %s, true, true)",
            (_MACHINE, str(home)),
        )
        conn.execute("INSERT INTO agents (id) VALUES (%s)", (_AGENT,))
        conn.execute(
            "INSERT INTO agents_meta (id, status, machine) VALUES (%s, 'running', %s)",
            (_AGENT, _MACHINE),
        )
    _live(born, live=True)
    state = Cycle(
        born=born,
        registry=registry,
        a=build_image(home, "a", extra=_MIGRATIONS),
        b=build_image(home, "b", extra=_MIGRATIONS),
        native=NativeStandIn(),
    )
    _point(home, state.a)
    # This interpreter boots as the home's anchored process tree.
    monkeypatch.setattr(dotenv_boot, "resolve_ava_home", lambda: (home, True))
    monkeypatch.setattr(dotenv_boot, "_HOME", home)
    monkeypatch.setattr(dotenv_boot, "_ANCHORED", True)
    monkeypatch.setattr(dotenv_boot, "AVA_ENV_PATH", home / ".env")
    monkeypatch.setattr(dotenv_boot, "AVA_MIRROR_ENV_PATH", home / "mirror.env")
    monkeypatch.setattr(dotenv_boot, "_db_authority_refusal", None)
    monkeypatch.setattr(connections, "_administrator_url", None)
    # The simulated fact: which image the running process is.
    admit = delivery.require_admitted_runtime

    def admitted(home: Path, *, code_root: Path, prefix: Path) -> None:
        del code_root, prefix
        assert state.running is not None
        site = state.site(state.running)
        admit(home, code_root=site, prefix=site.parents[2])

    def code_root() -> Path:
        assert state.running is not None
        return state.site(state.running)

    monkeypatch.setattr(delivery, "require_admitted_runtime", admitted)
    monkeypatch.setattr(entry, "_code_root", code_root)
    monkeypatch.setattr(linux, "_boot_id", lambda: "test-boot")

    def adapter(_request_or_record: object) -> NativeStandIn:
        return state.native

    monkeypatch.setattr(native, "for_host", adapter)
    monkeypatch.setattr(native, "for_launch", adapter)
    sink = logger.add(lambda message: state.exposed.append(str(message)), level="DEBUG")
    try:
        yield state
    finally:
        logger.remove(sink)


def _hand_off(
    cycle: Cycle, request: FleetRequest, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, str], set[str]]:
    """Process 1: the selected image's CLI hands the request to its executor.

    Returns the request document, the environment the exec carries and the
    keys it adds to or changes in the operator's own.
    """
    document = cycle.home.parent / f"request-{request.id}.json"
    document.write_text(request.model_dump_json())
    operator = {
        "HOME": os.environ["HOME"],
        "PATH": os.environ["PATH"],
        "AVA_HOME": str(cycle.home),
        "AVA_CLUSTER_REGISTRY": str(cycle.registry),
    }
    execs: list[tuple[Path, list[str], dict[str, str]]] = []

    def execve(path: str | Path, argv: list[str], environment: dict[str, str]) -> NoReturn:
        execs.append((Path(path), list(argv), dict(environment)))
        raise _Exec

    def chdir(_path: object) -> None:
        return  # the exec never happens, so the process keeps its directory

    cycle.running = request.previous  # the selected image: the home's admitted runtime
    with monkeypatch.context() as patch, _process(operator):
        patch.setattr(os, "execve", execve)
        patch.setattr(os, "chdir", chdir)
        with pytest.raises(_Exec):
            handoff.run(document)
    [(interpreter, argv, environment)] = execs
    assert interpreter == request.executor.verify(cycle.home).interpreter
    cycle.exposed += argv
    changed = {key for key in environment if environment[key] != operator.get(key)}
    return document, environment, changed


def _submit(
    cycle: Cycle,
    document: Path,
    environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Process 2: the executor image's `submit` entry, not selected yet."""
    request = FleetRequest.model_validate_json(document.read_bytes())
    cycle.running = request.executor
    with _process(environment):
        _normalize_process_profile()
        dotenv_boot.load_ava_env()  # its boot pass, at its Settings construction
        monkeypatch.setattr(settings.data_plane, "db_url", os.environ["AVA_DB_URL"])
        code = entry.main(["submit", str(document)])
    out, err = capsys.readouterr()
    cycle.exposed += [out, err]
    assert code == 0, err
    assert dotenv_boot.db_authority_refusal() is None
    operation = read_operation(request.path)
    assert (operation.phase, operation.direction) == ("prepared", "candidate")


def _execute(cycle: Cycle, request: FleetRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Process 3: the finite executor, in its launch plan's whole environment."""
    launch = read_operation(request.path).launch
    assert launch is not None
    environment = launch["environment"]
    assert isinstance(environment, dict)
    assert set(environment) == _FINITE_JOB_ENVIRONMENT
    cycle.running = request.executor
    with _process({key: str(value) for key, value in environment.items()}):
        dotenv_boot.load_ava_env()
        monkeypatch.setattr(settings.data_plane, "db_url", os.environ["AVA_DB_URL"])
        # The candidate is admitted to no generation; it dials as the administrator.
        refusal = dotenv_boot.db_authority_refusal()
        assert refusal is not None and "selected release image" in refusal
        adopt_executor_authority(cycle.home)
        with exclusive(request.path) as journal:
            Coordinator(journal, ImageGateway(request, cycle.born), RemoteUnits(request)).run()
    monkeypatch.setattr(connections, "_administrator_url", None)
    final = read_operation(request.path)
    assert final.fleet is not None and final.fleet.outcome == "clean", final.error
    assert current_pointer(cycle.home / "releases") == request.candidate.selector


def _release(
    cycle: Cycle,
    previous: ReleaseRef,
    candidate: ReleaseRef,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> str:
    """One release across its three processes; returns the delivered password."""
    request = cycle.request(previous, candidate)
    document, environment, changed = _hand_off(cycle, request, monkeypatch)
    delivered = authority.write_grant(cycle.home, "gateway")
    _submit(cycle, document, environment, monkeypatch, capsys)
    # Only the gateway login and its generation marker join the operator's
    # environment: no API token, no human secret.
    assert changed == {"AVA_DB_URL", authority.GENERATION_ENV}, changed
    assert environment["AVA_DB_URL"] == delivered.dsn(cycle.born.endpoint())
    assert environment[authority.GENERATION_ENV] == str(delivered.number)
    _execute(cycle, request, monkeypatch)
    return delivered.password


def test_each_release_crosses_its_image_boundary_with_exactly_the_authority_it_needs(
    cycle: Cycle, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A -> B -> A: the selected image hands its gateway login to the executor
    image's submission, the finite executor dials as the administrator, and
    each release admits the next write generation."""
    generations = [authority.active_generation(cycle.home).number]
    passwords = [_release(cycle, cycle.a, cycle.b, monkeypatch, capsys)]
    generations.append(authority.active_generation(cycle.home).number)
    passwords.append(_release(cycle, cycle.b, cycle.a, monkeypatch, capsys))
    generations.append(authority.active_generation(cycle.home).number)
    assert generations == [0, 1, 2]
    # `_release`'s returned password is each release's OUTGOING login (the one
    # handed to the executor before it mints the NEXT generation — see
    # `_release`'s docstring), so `passwords` above never actually captured
    # generation 2's own credential, minted at the end of the second release
    # and never returned anywhere. Read it directly so it is checked too.
    final_generation = authority.active_generation(cycle.home)
    final_secret = authority.read_secret(cycle.home, final_generation)
    passwords += [final_secret.roles.gateway.password, final_secret.roles.runner.password]
    state = read_state(cycle.home)
    assert state is not None and state.current.source_commit == cycle.a.source_commit
    # No login ever reached an argv, a native command, the journal, output or logs.
    assert len(cycle.native.commands) == 2
    for command in cycle.native.commands:
        cycle.exposed += command
    journals = [path.read_text() for path in (cycle.home / "updates").rglob("*") if path.is_file()]
    exposed = "\n".join([*cycle.exposed, *journals])
    for password in passwords:
        assert password not in exposed

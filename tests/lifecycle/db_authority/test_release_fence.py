"""A release's write-generation fence and admission on real PostgreSQL 17 and PgBouncer.

A single-box home born by the real start steps (`test_single_box.born`, empty
cluster secret) runs a real release journal through `authority.fence` and
`authority.authorize`: the effects are the real `write_generation` ones
(revoke + sweep, pooler stop with escalation, termination census, prune, mint,
direct proof, activate). The finite executor births no pooler: the stage's
ordinary start under the root boot owner does (`_ordinary_start`). Old writers
are held across the fence the ways stale code holds them. A process death is
injected after every catalog, pooler and ledger boundary; the continuation
must finish the same generation. The finite executor's own authority
(`adopt_executor_authority`) dials the owner-only socket acting as the gateway
group.
"""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from cli.commands.data_plane import bringup, write_generation
from cli.commands.data_plane import cluster_instance as ci
from cli.commands.data_plane import pgbouncer as pooler
from cli.release_fleet.request import FleetRequest
from cli.release_transition import authority as release_authority
from cli.release_transition.journal import Operation, create, exclusive, read_operation
from cli.release_transition.request import ReleaseRef
from shared import db_connections
from shared.cluster import authority, ownership
from shared.config import settings
from shared.host.env import dotenv_boot
from shared.host.net.url_secret import url_with_userinfo
from tests.lifecycle.db_authority.test_single_box import Born, _refused
from tests.lifecycle.db_authority.test_single_box import born as born
from tests.lifecycle.db_authority.test_single_box import configured as configured
from tests.lifecycle.transition.phases import advance_to

pytestmark = pytest.mark.skipif(
    not (Path(pooler.pgbouncer_bin()).exists() or shutil.which(pooler.pgbouncer_bin())),
    reason="pgbouncer not installed (brew/apt)",
)

_PREVIOUS = ReleaseRef(
    artifact_digest="a" * 64,
    manifest_digest="b" * 64,
    schema_digest="c" * 64,
    source_commit="d" * 40,
)
_CANDIDATE = _PREVIOUS.model_copy(update={"artifact_digest": "e" * 64, "source_commit": "9" * 40})


def _point(home: Path, reference: ReleaseRef) -> None:
    (home / "releases/current-release").write_text(
        json.dumps(
            {
                "artifact_digest": reference.artifact_digest,
                "manifest_digest": reference.manifest_digest,
            }
        )
    )


@pytest.fixture
def release(born: Born) -> FleetRequest:
    (born.home / "releases").mkdir()
    _point(born.home, _PREVIOUS)
    request = FleetRequest(
        id=uuid4(),
        home=str(born.home),
        registry=str(born.home.parent / "clusters.json"),
        created_at=datetime.now(UTC),
        machine="test",
        previous=_PREVIOUS,
        candidate=_CANDIDATE,
        executor=_CANDIDATE,
        configuration_digest="f" * 64,
    )
    create(request)
    return request


@dataclass(frozen=True)
class Login:
    name: str
    password: str


def _logins(born: Born) -> dict[str, Login]:
    secret = authority.read_secret(born.home, authority.active_generation(born.home))
    return {
        cls: Login(secret.roles.of(cls).name, secret.roles.of(cls).password)
        for cls in ("gateway", "runner")
    }


def _refused_everywhere(born: Born, login: Login) -> None:
    """A fenced login fails over direct TCP, the owner-only socket and the pooler."""
    for host, port in (
        ("127.0.0.1", born.pg_port),
        (str(ci._pg_socket_dir()), born.pg_port),
        ("127.0.0.1", born.pooler_port),
    ):
        _refused(host=host, port=port, user=login.name, password=login.password, dbname="ava")


def _fence(request: FleetRequest) -> Operation:
    with exclusive(request.path) as journal:
        if journal.operation.phase != "fencing":
            advance_to(journal, "fencing")
        release_authority.fence(journal)
        return journal.operation


def _authorize(request: FleetRequest, target: ReleaseRef) -> Operation:
    with exclusive(request.path) as journal:
        if journal.operation.phase == "fencing":
            journal.advance("selecting")
        _point(Path(request.home), target)
        if journal.operation.phase == "selecting":
            journal.advance("authorizing")
        release_authority.authorize(journal, target)
        return journal.operation


def _pooler_birth() -> Any:
    return ownership.pooler(pooler.ini_path(), pooler.pidfile_path())


def _ordinary_start(born: Born) -> None:
    """The stage's data-plane step (`complete_gateway_data_plane`), which runs
    under the root boot owner: a pooler serving exactly the active pair."""
    bringup._ensure_pooler(born.record, "ava", born.home, authority.active_generation(born.home))


def test_preflight_admits_exactly_one_served_generation(born: Born) -> None:
    assert write_generation.preflight_write_authority().number == 0
    userlist = born.home / "pgbouncer" / "userlist.txt"
    served = userlist.read_bytes()
    userlist.write_bytes(served.splitlines(keepends=True)[0])
    with pytest.raises(authority.AuthorityRefusedError, match="does not serve exactly"):
        write_generation.preflight_write_authority()
    userlist.write_bytes(served)
    with born.admin() as conn:
        conn.execute("CREATE ROLE stray_writer LOGIN PASSWORD 'stray-password' IN ROLE ava_runner")
    with pytest.raises(authority.CatalogRefusedError, match="stray_writer"):
        write_generation.preflight_write_authority()


@contextmanager
def _stale_writers(born: Born, old: dict[str, Login]) -> Generator[Callable[[], None]]:
    """Writers of the active generation, held the ways old code holds them: an
    open transaction through the pooler, a running statement over TCP, and a
    client pool. The yielded check proves each one was closed."""
    from psycopg_pool import ConnectionPool

    open_transaction = psycopg.connect(born.dsn("gateway"), prepare_threshold=None)
    open_transaction.execute("INSERT INTO agents (id) VALUES (930001)")
    runner = old["runner"]
    sleeping = psycopg.connect(
        host="127.0.0.1",
        port=born.pg_port,
        user=runner.name,
        password=runner.password,
        dbname="ava",
        autocommit=True,
    )
    ended: list[BaseException] = []

    def sleep() -> None:
        try:
            sleeping.execute("SELECT pg_sleep(60)")
        except BaseException as exc:
            ended.append(exc)

    sleeper = threading.Thread(target=sleep)
    sleeper.start()
    pool = ConnectionPool(
        born.dsn("runner"),
        min_size=2,
        max_size=2,
        open=True,
        kwargs={"prepare_threshold": None, "autocommit": True},
    )
    pool.wait(timeout=10)

    def closed() -> None:
        sleeper.join(timeout=15)
        assert not sleeper.is_alive() and ended
        with pytest.raises(psycopg.OperationalError):
            open_transaction.commit()
        with pytest.raises(psycopg.OperationalError), pool.connection(timeout=5) as conn:
            conn.execute("SELECT 1")

    try:
        yield closed
    finally:
        pool.close()
        open_transaction.close()
        sleeping.close()
        sleeper.join(timeout=15)


def test_fence_closes_every_stale_writer_before_any_new_generation(
    born: Born, release: FleetRequest
) -> None:
    old = _logins(born)
    with _stale_writers(born, old) as writers_closed:
        fenced = _fence(release)
        writers_closed()
    record = fenced.fence("candidate")
    assert record is not None and record.state == "closed" and record.evidence is not None
    assert record.generation.number == 0
    # The open pooled transaction holds a server connection, so the safe
    # shutdown cannot finish: the fence escalated (a reload never revokes).
    assert record.evidence.pooler == "forced"
    assert record.evidence.terminated >= 1 and record.evidence.dropped
    assert set(record.generation.roles) <= set(record.evidence.roles)
    ledger = authority.require_ledger(born.home)
    assert ledger.active is None
    assert [(e.number, e.state, e.dropped) for e in ledger.revoked] == [(0, "closed", True)]
    assert not (authority.authority_dir(born.home) / "generations" / "0.json").exists()
    assert not pooler.pgbouncer_listener_reachable(born.pooler_port, "no-pooler-answers")
    with born.admin() as conn:
        names = [login.name for login in old.values()]
        dropped = conn.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (names,))
        assert dropped.fetchall() == []
    for login in old.values():
        _refused(
            host="127.0.0.1",
            port=born.pg_port,
            user=login.name,
            password=login.password,
            dbname="ava",
        )


def test_the_admitted_generation_is_the_only_writer_behind_a_fresh_pooler(
    born: Born, release: FleetRequest
) -> None:
    old = _logins(born)
    before = _pooler_birth()
    with psycopg.connect(born.dsn("gateway"), prepare_threshold=None) as uncommitted:
        uncommitted.execute("INSERT INTO agents (id) VALUES (930002)")
        _fence(release)
        with pytest.raises(psycopg.OperationalError):
            uncommitted.commit()
    admitted = _authorize(release, _CANDIDATE)
    issue = admitted.issue("candidate")
    assert issue is not None and issue.state == "authorized" and issue.number == 1
    assert issue.generation is not None
    # The finite executor births no long-lived data-plane process: a pooler it
    # forked would share the executor's native custody (its systemd cgroup,
    # KillMode=control-group) and die when the executor exits.
    assert _pooler_birth() is None
    assert not pooler.pgbouncer_listener_reachable(born.pooler_port, "no-pooler-answers")
    _ordinary_start(born)
    after = _pooler_birth()
    assert before is not None and after is not None and after.pid != before.pid
    new = _logins(born)
    assert {login.name for login in new.values()} == {"ava_g1_gateway", "ava_g1_runner"}
    for login in old.values():
        _refused_everywhere(born, login)
    with psycopg.connect(born.dsn("gateway"), prepare_threshold=None, autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM agents WHERE id = 930002").fetchone() == (0,)
    write_generation.verify_write_generation(1, issue.generation.credential_digest)


def test_failed_candidate_is_fenced_and_the_predecessor_runs_on_a_new_generation(
    born: Born, release: FleetRequest
) -> None:
    """The data-plane A/B/A: G0 -> G1 for the candidate -> G2 for the predecessor."""
    generation_zero = _logins(born)
    _fence(release)
    _authorize(release, _CANDIDATE)
    _ordinary_start(born)
    generation_one = _logins(born)
    held = psycopg.connect(born.dsn("runner"), prepare_threshold=None)
    held.execute("SELECT 1")
    with exclusive(release.path) as journal:
        advance_to(journal, "starting")
        journal.recover("candidate readiness failed", at=datetime.now(UTC))
        journal.advance("fencing")
    recovered = _fence(release)
    with pytest.raises(psycopg.OperationalError):
        held.execute("SELECT 1")
    held.close()
    final = _authorize(release, _PREVIOUS)
    assert [(f.direction, f.generation.number) for f in final.db_fences] == [
        ("candidate", 0),
        ("previous", 1),
    ]
    assert [(i.direction, i.number) for i in final.db_issues] == [
        ("candidate", 1),
        ("previous", 2),
    ]
    assert recovered.fence("previous") is not None
    _ordinary_start(born)
    for login in (*generation_zero.values(), *generation_one.values()):
        _refused_everywhere(born, login)
    ledger = authority.require_ledger(born.home)
    assert ledger.active is not None and ledger.active.number == 2
    assert ledger.active.origin.direction == "previous"
    with exclusive(release.path) as journal:
        journal.advance("starting")
        release_authority.require_issued(journal.operation)
        release_authority.verify_active(journal.operation)


class ControllerLost(BaseException):
    """A process death cannot run the executor's exception compensation."""


def _dies_after(module: Any, name: str) -> Callable[[pytest.MonkeyPatch], None]:
    def install(monkeypatch: pytest.MonkeyPatch) -> None:
        real = getattr(module, name)

        def effect(*args: Any, **kwargs: Any) -> Any:
            real(*args, **kwargs)
            monkeypatch.setattr(module, name, real)
            raise ControllerLost(name)

        monkeypatch.setattr(module, name, effect)

    return install


_FENCE_DEATHS = {
    "revoked": _dies_after(authority, "revoke"),
    "pooler-stopped": _dies_after(pooler, "stop_pgbouncer"),
    "closed": _dies_after(authority, "close_revoked"),
    "pruned": _dies_after(authority, "prune"),
}
_ISSUE_DEATHS = {
    "minted": _dies_after(authority, "mint_generation"),
    "proven": _dies_after(bringup, "prove_generation_logins"),
    "activated": _dies_after(authority, "activate"),
}


@pytest.mark.parametrize("death", [*_FENCE_DEATHS, *_ISSUE_DEATHS])
def test_death_after_each_boundary_continues_the_same_generation(
    born: Born, release: FleetRequest, monkeypatch: pytest.MonkeyPatch, death: str
) -> None:
    old = _logins(born)
    if death in _FENCE_DEATHS:
        _FENCE_DEATHS[death](monkeypatch)
        with pytest.raises(ControllerLost):
            _fence(release)
        interrupted = read_operation(release.path).fence("candidate")
        assert interrupted is not None and interrupted.state == "revoking"
        _fence(release)
    else:
        _fence(release)
        _ISSUE_DEATHS[death](monkeypatch)
        with pytest.raises(ControllerLost):
            _authorize(release, _CANDIDATE)
        interrupted_issue = read_operation(release.path).issue("candidate")
        assert interrupted_issue is not None and interrupted_issue.state == "minting"
    final = _authorize(release, _CANDIDATE)
    issue = final.issue("candidate")
    assert issue is not None and issue.generation is not None and issue.number == 1
    ledger = authority.require_ledger(born.home)
    assert ledger.counter == 1 and ledger.active is not None
    assert (ledger.active.number, ledger.active.credential_digest) == (
        1,
        issue.generation.credential_digest,
    )
    _ordinary_start(born)
    for login in old.values():
        _refused_everywhere(born, login)
    write_generation.verify_write_generation(1, issue.generation.credential_digest)


def test_a_changed_verifier_after_a_death_holds_instead_of_minting_again(
    born: Born, release: FleetRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fence(release)
    _ISSUE_DEATHS["minted"](monkeypatch)
    with pytest.raises(ControllerLost):
        _authorize(release, _CANDIDATE)
    with born.admin() as conn:
        conn.execute("ALTER ROLE ava_g1_runner PASSWORD 'replaced-underneath-the-mint'")
    with pytest.raises(authority.CatalogRefusedError, match="stored verifier differs"):
        _authorize(release, _CANDIDATE)
    ledger = authority.require_ledger(born.home)
    assert ledger.counter == 1 and ledger.active is None
    held = read_operation(release.path)
    issue = held.issue("candidate")
    assert held.phase == "authorizing" and issue is not None and issue.state == "minting"


def test_a_surviving_session_holds_the_fence_without_a_closure_receipt(
    born: Born, release: FleetRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared.cluster.authority import fence as library_fence

    def unconfirmed(_conn: Any, _pid: int, _timeout_ms: int) -> bool:
        return False  # the signal is never confirmed, so the session stays

    old = _logins(born)
    survivor = psycopg.connect(
        host="127.0.0.1",
        port=born.pg_port,
        user=old["gateway"].name,
        password=old["gateway"].password,
        dbname="ava",
        autocommit=True,
    )
    monkeypatch.setattr(library_fence, "_terminate", unconfirmed)
    try:
        with pytest.raises(authority.ClosureRefusedError, match="sessions survived"):
            _fence(release)
    finally:
        survivor.close()
    held = read_operation(release.path).fence("candidate")
    assert held is not None and held.state == "revoking" and held.evidence is None
    ledger = authority.require_ledger(born.home)
    assert [(entry.number, entry.state) for entry in ledger.revoked] == [(0, "revoking")]


@pytest.fixture
def executor(born: Born, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The finite executor's adopted authority; restored after the test."""
    monkeypatch.setattr(db_connections, "_administrator_url", None)
    monkeypatch.setattr(settings.data_plane, "db_url", settings.data_plane.db_url)
    release_authority.adopt_executor_authority(born.home)
    yield settings.data_plane.db_url


def test_executor_dials_the_owner_socket_acting_as_the_gateway_group(
    born: Born, executor: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shared.db

    # The boot pass refused the candidate image any generation login.
    monkeypatch.setattr(dotenv_boot, "db_authority_refusal", lambda: "candidate image")
    # A socket URL names no port to swap: the admin dial is already direct.
    assert shared.db.direct_db_url() == executor
    for dial in (shared.db.connect, lambda: shared.db.connect(direct=True)):
        with dial() as conn:
            identity = conn.execute("SELECT session_user, current_user").fetchone()
            assert identity is not None and identity[1] == "ava_gateway"
            assert conn.execute("SHOW statement_timeout").fetchone() == ("1min",)
            conn.execute("RESET ALL")
            assert conn.execute("SELECT current_user").fetchone() == ("ava_gateway",)
            conn.execute("INSERT INTO agents (id) VALUES (940001)")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("CREATE TABLE executor_must_not_create (id int)")
            conn.rollback()
    with shared.db.pool(min_size=1, max_size=1) as pool, pool.connection() as conn:
        assert conn.execute("SELECT current_user").fetchone() == ("ava_gateway",)
    # Only the adopted URL is exempt from the refusal; the endpoint is not.
    with pytest.raises(db_connections.NoDatabaseAuthorityError):
        db_connections._guard_db_url(born.endpoint())
    with pytest.raises(ValueError, match="password-free owner-only socket"):
        db_connections.adopt_administrator(url_with_userinfo(born.endpoint(), "ava", "pw"))


def test_the_fence_never_terminates_the_executors_own_sessions(
    born: Born, release: FleetRequest, executor: str
) -> None:
    import shared.db

    del executor
    with shared.db.connect(autocommit=True) as conn:
        _fence(release)
        assert conn.execute("SELECT current_user").fetchone() == ("ava_gateway",)


def test_observation_refuses_a_surviving_fenced_session(born: Born, release: FleetRequest) -> None:
    """A session the fence would close (here: of a role that lost LOGIN while
    connected) makes observation hold, even though every login answers."""
    _fence(release)
    admitted = _authorize(release, _CANDIDATE)
    _ordinary_start(born)
    issue = admitted.issue("candidate")
    assert issue is not None and issue.generation is not None
    digest = issue.generation.credential_digest
    write_generation.verify_write_generation(1, digest)
    with born.admin() as conn:
        conn.execute("CREATE ROLE lingering LOGIN PASSWORD 'lingering-password-xyz'")
    with psycopg.connect(
        host="127.0.0.1",
        port=born.pg_port,
        user="lingering",
        password="lingering-password-xyz",  # noqa: S106 — a throwaway role's fixture credential
        dbname="postgres",  # the census covers every database of the cluster
        autocommit=True,
    ) as lingering:
        with born.admin() as conn:
            conn.execute("ALTER ROLE lingering NOLOGIN PASSWORD NULL")
        with pytest.raises(authority.AuthorityRefusedError, match="sessions of fenced roles"):
            write_generation.verify_write_generation(1, digest)
        assert lingering.execute("SELECT 1").fetchone() == (1,)  # observed, never terminated


def test_preview_stale_writer_probe_proves_the_fence(born: Born, release: FleetRequest) -> None:
    """The preview A/B/A's probe (scripts/preview/release_generation.py) on a real
    fence: a writer outside root custody holding generation 0 is terminated, its
    transaction aborts, it never commits again, and every generation-0 login is
    refused over TCP, the owner-only socket and the pooler."""
    from scripts.preview import release_generation

    run = born.home.parent
    (run / "config.json").write_text(
        json.dumps({"ports": {"postgres": born.pg_port, "pgbouncer": born.pooler_port}})
    )
    context = release_generation.Context(run)
    fenced_logins = _logins(born)
    try:
        release_generation.capture(context, "ab")
        assert (run / "generation-ab.json").stat().st_mode & 0o077 == 0
        release_generation.probe(context, "ab")
        _fence(release)
        _authorize(release, _CANDIDATE)
        _ordinary_start(born)
        release_generation.fenced(context, "ab")
    finally:
        release_generation.stop(context, "all")
    report = json.loads((run / "fence-ab.json").read_text())
    assert report["result"] == "passed" and report["generation"] == 0
    assert report["probe"]["held_transaction"] == "aborted"
    assert report["probe"]["refused_reconnects"] >= 1
    assert sorted(report["refused"]) == sorted(
        f"{cls}/{route}" for cls in ("gateway", "runner") for route in ("tcp", "socket", "pooler")
    )
    assert not (run / "generation-ab.json").exists()
    written = (run / "fence-ab.json").read_text() + (run / "stale-writer-ab.jsonl").read_text()
    assert all(login.password not in written for login in fenced_logins.values())


def test_preview_observer_reads_stored_agents_as_the_administrator_across_rotations(
    born: Born, release: FleetRequest
) -> None:
    """The preview observer runs from the source checkout, which is not the
    admitted runtime once a release image is selected: it reads the retained
    agents over the owner-only socket, before and after a rotation."""
    from scripts.preview import linux_observer

    run = born.home.parent
    (run / "config.json").write_text(
        json.dumps({"ports": {"postgres": born.pg_port, "pgbouncer": born.pooler_port}})
    )
    with psycopg.connect(born.dsn("gateway"), prepare_threshold=None, autocommit=True) as conn:
        conn.execute("INSERT INTO agents (id) VALUES (950001)")
    (run / "smoke-release-a.json").write_text(json.dumps({"agent": 950001}))
    assert linux_observer._stored_agents(run) == [950001]
    _fence(release)
    _authorize(release, _CANDIDATE)
    assert linux_observer._stored_agents(run) == [950001]


def test_preview_state_reads_retained_work_as_the_administrator_across_rotations(
    born: Born, release: FleetRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preview's completed-work comparison (scripts/preview/release_cycle_state.py)
    runs from the source checkout, which holds only the credential-free endpoint
    once a release image is selected. It reads the retained agent over the
    owner-only socket, identically before and after a rotation, and cannot write."""
    from scripts.preview import release_cycle_state, release_generation

    run = born.home.parent
    (run / "config.json").write_text(
        json.dumps({"ports": {"postgres": born.pg_port, "pgbouncer": born.pooler_port}})
    )
    with psycopg.connect(born.dsn("gateway"), prepare_threshold=None, autocommit=True) as conn:
        conn.execute("INSERT INTO agents (id) VALUES (950002)")
        conn.execute("INSERT INTO agents_meta (id, status) VALUES (950002, 'terminated')")
        conn.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_id, checkpoint)"
            " VALUES ('950002', 'retained', '{}')"
        )
    monkeypatch.setattr(settings.data_plane, "db_url", born.endpoint())
    before = release_cycle_state.state(run, 950002)
    assert before["rows"] == {"checkpoints": 1, "checkpoint_blobs": 0, "checkpoint_writes": 0}
    _fence(release)
    _authorize(release, _CANDIDATE)
    assert release_cycle_state.state(run, 950002) == before
    with (
        release_generation.Context(run).read_only() as conn,
        pytest.raises(psycopg.errors.ReadOnlySqlTransaction),
    ):
        conn.execute("DELETE FROM checkpoints WHERE thread_id = '950002'")

"""Real process quiescence and the credential pipe's birth-registration boundary."""

from __future__ import annotations

import contextlib
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import psutil
import pytest

from agent import impersonation
from base.agents.observation.relay_supervision import relay_exited
from base.config.service_read import ConfigAuthority
from base.native_process.ownership import OwnedProcess


@pytest.mark.parametrize("wrong_birth", [False, True])
def test_recorded_birth_retirement_prevents_new_old_sender_submission(
    tmp_path: Path, wrong_birth: bool
) -> None:
    marker = tmp_path / "submitted"
    process = subprocess.Popen(  # noqa: S603 — fixed isolated test child
        [
            sys.executable,
            "-c",
            'import sys; from pathlib import Path; sys.stdin.buffer.read(1); Path(sys.argv[1]).write_text("submitted")',
            str(marker),
        ],
        stdin=subprocess.PIPE,
    )
    try:
        identity = OwnedProcess.capture(psutil.Process(process.pid))
        if wrong_birth:
            identity = OwnedProcess(
                identity.pid,
                identity.birth + 1,
                identity.starttime + 1 if identity.starttime is not None else None,
            )
        assert relay_exited(None, asdict(identity)) is wrong_birth
        assert impersonation._retire_recorded_relay(
            {"relay_identity": asdict(identity), "relay_generation": 1}
        )
        if wrong_birth:
            assert process.poll() is None  # Never signal a reused or mismatched birth.
        else:
            process.wait(timeout=5)
            assert process.stdin is not None
            with pytest.raises(BrokenPipeError):
                process.stdin.write(b"x")
                process.stdin.flush()
            assert not marker.exists()
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        if process.stdin is not None:
            with contextlib.suppress(BrokenPipeError):
                process.stdin.close()


def test_unknown_retirement_does_not_signal_live_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        identity = OwnedProcess.capture(psutil.Process(process.pid))
        monkeypatch.setattr(
            OwnedProcess, "live", Mock(side_effect=RuntimeError("birth unavailable"))
        )
        assert not relay_exited(None, asdict(identity))
        assert not impersonation._retire_recorded_relay(
            {"relay_identity": asdict(identity), "relay_generation": 1}
        )
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.parametrize("registration_fails", [False, True])
def test_spawn_never_releases_private_token_before_birth_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registration_fails: bool,
) -> None:
    marker = tmp_path / "received"
    real_popen = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def spawn(_argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        child = real_popen(
            [
                sys.executable,
                "-c",
                "import sys; from pathlib import Path; token=sys.stdin.buffer.readline(); "
                "Path(sys.argv[1]).write_bytes(token) if token else None",
                str(marker),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            start_new_session=True,
            env=kwargs["env"],
        )
        children.append(child)
        return child

    monkeypatch.setattr(impersonation.subprocess, "Popen", spawn)
    identities: list[OwnedProcess] = []

    def register(child: subprocess.Popen[bytes]) -> None:
        assert not marker.exists()
        identities.append(OwnedProcess.capture(psutil.Process(child.pid)))
        if registration_fails:
            raise RuntimeError("DB disconnected before birth persistence")

    if registration_fails:
        with pytest.raises(RuntimeError, match="DB disconnected"):
            impersonation._spawn_codex_relay(
                1, "lease", "private-test-token", "thread", None, register=register
            )
        assert not marker.exists()
    else:
        child = impersonation._spawn_codex_relay(
            1, "lease", "private-test-token", "thread", None, register=register
        )
        child.wait(timeout=5)
        assert marker.read_bytes() == b"private-test-token\n"
    assert len(identities) == len(children) == 1
    assert children[0].poll() is not None


def test_new_claim_without_birth_is_safe_but_legacy_unknown_is_not() -> None:
    assert not relay_exited(None, None), "missing birth is not confirmed exit"
    assert impersonation._retire_recorded_relay({"relay_identity": None, "relay_generation": 1})
    assert not impersonation._retire_recorded_relay({"relay_identity": None, "relay_generation": 0})


@pytest.fixture
def recovery_lease(
    db_conn: Any,
    database: Any,
    event_bus: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config_authority: ConfigAuthority,
) -> Any:
    import tempfile
    from uuid import uuid4

    from base.agents import impersonation as leases
    from base.agents.impersonation.tests.test_host_transport import FakeAppServer
    from base.agents.messages.caller_identity import CallerIdentity
    from base.cluster.machine import machine_name
    from base.db import create_agent
    from base.db.test_db_guard import assert_test_db_url
    from base.native_process.runtime_incarnation import RuntimeIncarnation
    from base.sessions import env_forwarding
    from tests.impersonation_support import recorded_tree

    # The real relay boots this test-owned unit instead of inheriting cluster secrets.
    url = config_authority.runtime.data_plane.db_url
    assert_test_db_url(url, context="real impersonation relay fixture")
    config_authority.env_path.write_text(
        f"AVA_DB_URL={url}\n"
        f"AVA_REDIS_URL={config_authority.runtime.data_plane.redis_url}\n"
        f"AVA_MACHINE_NAME={machine_name()}\n",
        encoding="utf-8",
    )

    # Test-only gateway isolation is the same skip posture as env_bootstrap;
    # production still re-sources its admitted unit configuration normally.
    forwarded = env_forwarding.forward_env_dict()
    monkeypatch.setattr(
        env_forwarding,
        "forward_env_dict",
        Mock(return_value=forwarded | {"AVA_CONFIG_FETCH": "skip"}),
    )
    aid = create_agent(db_conn)
    owner = RuntimeIncarnation(aid, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,runtime_kind,lease_expires_at) "
        "VALUES(%s,'idling',%s,%s,%s,'process',clock_timestamp()+interval '1 hour')",
        (aid, machine_name(), owner.generation, owner.owner),
    )
    db_conn.commit()
    with tempfile.TemporaryDirectory(prefix="cxre-", dir="/tmp") as directory:
        socket = Path(directory) / "host.sock"
        server = FakeAppServer()
        server.start(socket)
        try:
            session = leases.request(
                database,
                event_bus,
                aid,
                caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
                ttl_seconds=3600,
                reason="Recover transport",
                relay_provider="codex",
                relay_thread_id=str(uuid4()),
                relay_codex_remote=f"unix://{socket}",
                process_metadata=recorded_tree(),
                authority=config_authority,
            )
            leases.accept(database, event_bus, session["id"], aid, owner, "Continue pending work")
            leases.activate(database, event_bus, session["id"], owner)
            yield session, owner
        finally:
            server.stop()


async def test_confirmed_child_death_recovers_same_lease_after_db_disconnect(
    db_conn: Any,
    database: Any,
    event_bus: Any,
    monkeypatch: pytest.MonkeyPatch,
    recovery_lease: Any,
) -> None:
    import asyncio
    from datetime import UTC, datetime, timedelta

    import psycopg
    from psycopg.types.json import Jsonb

    from base.agents import impersonation as leases
    from base.agents.observation.relay_supervision import RelaySupervision
    from tests.impersonation_support import attested_caller

    session, owner = recovery_lease
    assert leases.provision_relay(database, session["id"], owner, "old-private") is not None
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    identity = OwnedProcess.capture(psutil.Process(dead.pid))
    dead.wait(timeout=5)
    db_conn.execute(
        "UPDATE agent_impersonations SET relay_identity=%s,relay_minted_at=clock_timestamp(),relay_heartbeat_at=clock_timestamp() WHERE id=%s",
        (Jsonb(asdict(identity)), session["id"]),
    )
    db_conn.commit()
    current = leases.native_status(database, event_bus, owner.agent_id, owner)
    assert current is not None
    assert impersonation._heartbeat_fresh(current["relay_heartbeat_at"])
    expires_at = current["expires_at"]
    monkeypatch.setattr(impersonation, "_provider_anchor_states", Mock(return_value=["alive"]))
    relays = RelaySupervision()
    original = leases.provision_relay
    monkeypatch.setattr(
        leases,
        "provision_relay",
        Mock(side_effect=psycopg.errors.ConnectionTimeout("DB unavailable")),
    )
    with pytest.raises(psycopg.errors.ConnectionTimeout):
        await impersonation.supervise_relay(
            database, event_bus, current, owner.agent_id, relays, incarnation=owner
        )
    assert (
        leases.get(database, event_bus, session["id"], attested_caller(session))["status"]
        == "active"
    )
    monkeypatch.setattr(leases, "provision_relay", original)
    spawn = Mock(wraps=impersonation._spawn_codex_relay)
    monkeypatch.setattr(impersonation, "_spawn_codex_relay", spawn)
    await asyncio.gather(
        *(
            impersonation.supervise_relay(
                database, event_bus, current, owner.agent_id, relays, incarnation=owner
            )
            for _ in range(2)
        )
    )
    assert spawn.call_count == 1
    child = relays.children[owner.agent_id]
    deadline = datetime.now(UTC) + timedelta(seconds=10)
    try:
        live = leases.get(database, event_bus, session["id"], attested_caller(session))
        while datetime.now(UTC) < deadline and live["relay_heartbeat_at"] is None:
            await asyncio.sleep(0.05)
            live = leases.native_status(database, event_bus, owner.agent_id, owner)
            assert live is not None
        assert (live["status"], live["relay_generation"]) == ("active", 2)
        assert live["relay_heartbeat_at"] is not None
        assert live["relay_identity"]["pid"] == child.process.pid
        assert live["expires_at"] == expires_at
        with pytest.raises(leases.ImpersonationError, match="Invalid relay token"):
            leases.relay_heartbeat(database, session["id"], "old-private")
    finally:
        impersonation._terminate_relay(child)

"""The relay credential pipe and the real database effect of a dead relay child."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest

from agent import impersonation, impersonation_process
from agent.tests.impersonation.test_impersonation import gate_ctx as gate_ctx
from agent.tests.impersonation.test_impersonation import relays as relays
from base.agents.context import AvaContext
from base.agents.impersonation import terminal_notices
from base.agents.observation.relay_supervision import RelayChild
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority


def test_spawn_sends_the_private_token_over_the_pipe_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "received"
    real_popen = subprocess.Popen
    argvs: list[list[str]] = []

    def spawn(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        argvs.append(argv)
        return real_popen(
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

    monkeypatch.setattr(impersonation_process.subprocess, "Popen", spawn)
    child = impersonation_process.spawn_codex_relay(
        1, "lease", "private-test-token", "thread", None
    )
    child.wait(timeout=5)
    assert marker.read_bytes() == b"private-test-token\n"
    assert all("private-test-token" not in part for part in argvs[0])


@pytest.fixture
def active_codex_lease(
    db_conn: Any,
    database: Any,
    event_bus: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> Any:
    import tempfile
    from uuid import uuid4

    from base.agents import impersonation as leases
    from base.agents.impersonation.tests.impersonation_support import recorded_tree
    from base.agents.impersonation.tests.test_host_transport import FakeAppServer
    from base.api_contracts.caller_identity import CallerIdentity
    from base.db import create_agent
    from base.db.test_db_guard import assert_test_db_url
    from base.native_process.runtime_incarnation import RuntimeIncarnation
    from base.sessions import env_forwarding

    # The real relay boots this test-owned unit instead of inheriting cluster secrets.
    url = config_authority.runtime.data_plane.db_url
    assert_test_db_url(url, context="real impersonation relay fixture")
    config_authority.env_path.write_text(
        f"AVA_DB_URL={url}\n"
        f"AVA_REDIS_URL={config_authority.runtime.data_plane.redis_url}\n"
        f"AVA_MACHINE_NAME={machine_identity.name()}\n",
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
        (aid, machine_identity.name(), owner.generation, owner.owner),
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
                reason="Degrade transport",
                relay_provider="codex",
                relay_thread_id=str(uuid4()),
                relay_codex_remote=f"unix://{socket}",
                process_metadata=recorded_tree(),
                authority=config_authority,
                local_machine=machine_identity.name(),
            )
            leases.accept(
                database,
                event_bus,
                session["id"],
                aid,
                owner,
                "Continue pending work",
                local_machine=machine_identity.name(),
            )
            leases.activate(database, event_bus, session["id"], owner)
            yield session, owner
        finally:
            server.stop()


async def test_dead_relay_child_is_degraded_and_never_replaced(
    db_conn: Any,
    database: Any,
    event_bus: Any,
    monkeypatch: pytest.MonkeyPatch,
    active_codex_lease: Any,
    *,
    machine_identity: MachineIdentity,
) -> None:
    from base.agents import impersonation as leases
    from base.agents.observation.relay_supervision import RelayChild, RelaySupervision

    session, owner = active_codex_lease
    assert leases.provision_relay(database, session["id"], owner, "old-private") is not None
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=5)
    db_conn.execute(
        "UPDATE agent_impersonations SET relay_heartbeat_at=clock_timestamp() WHERE id=%s",
        (session["id"],),
    )
    db_conn.commit()
    current = leases.native_status(database, event_bus, owner.agent_id, owner)
    assert current is not None
    spawn = Mock()
    monkeypatch.setattr(impersonation_process, "spawn_codex_relay", spawn)
    relays = RelaySupervision()
    relays.children[owner.agent_id] = RelayChild(
        session["id"], dead, "old-private", 0.0, current["relay_generation"]
    )
    await impersonation.supervise_relay(
        database, event_bus, current, owner.agent_id, relays, incarnation=owner
    )
    spawn.assert_not_called()
    after = leases.get(
        database,
        event_bus,
        session["id"],
        local_machine=machine_identity.name(),
    )
    assert after["status"] == "active"
    assert after["relay_generation"] == current["relay_generation"]
    assert after["relay_degraded_reason"] == "relay unavailable; automatic recovery unsupported"
    assert after["relay_degraded_at"] is not None
    assert after["expires_at"] == current["expires_at"]
    leases.relay_heartbeat(
        database, session["id"], "old-private", local_machine=machine_identity.name()
    )  # the credential is unchanged


@pytest.mark.parametrize("error", [None, "TimeoutError: ambiguous acceptance"])
async def test_ended_lease_notice_is_submitted_once_and_a_failure_is_reported_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    gate_ctx: AvaContext,
    loguru_records: list[dict[str, Any]],
    error: str | None,
) -> None:
    lease_id = "00000000-0000-0000-0000-000000000001"
    gate_ctx.relays.children[42] = RelayChild(lease_id, MagicMock(), "token", 0.0, 1)
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = ("expired",)
    db = MagicMock()
    db.connect.return_value.__enter__.return_value = conn
    monkeypatch.setattr(impersonation, "terminate_relay", Mock())
    send = Mock(return_value=error)
    monkeypatch.setattr(terminal_notices, "send_ended_notice", send)
    # Every later pass finds no child left to announce for: there is no retry.
    for _ in range(3):
        await impersonation.supervise_relay(
            db, MagicMock(), None, 42, gate_ctx.relays, incarnation=None
        )
    send.assert_called_once_with(db, lease_id)
    failures = [
        record["extra"]
        for record in loguru_records
        if record["extra"].get("event") == "impersonation_terminal_notice_failed"
    ]
    assert [failure["error"] for failure in failures] == ([] if error is None else [error])
    assert 42 not in gate_ctx.relays.children

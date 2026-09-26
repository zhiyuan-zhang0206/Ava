"""Caller attestation on the controller surface: tiers, pid reuse, generation crossing."""

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psutil
import pytest

from shared.agents import impersonation as leases
from shared.agents.impersonation import _impersonation_store as store
from shared.caller_identity import CallerIdentity
from shared.db import create_agent
from shared.machine import machine_name
from shared.runtime_incarnation import RuntimeIncarnation
from tests.impersonation_support import attested_caller, recorded_tree, unrelated_caller


def _stub_birth(monkeypatch: pytest.MonkeyPatch, birth: float) -> None:
    def stable(_process: object) -> float:
        return birth

    monkeypatch.setattr(store, "stable_create_time", stable)


def _lease(tree: dict[str, Any]) -> dict[str, Any]:
    return {"process_metadata": tree, "machine": machine_name()}


def _stub_liveness(
    monkeypatch: pytest.MonkeyPatch, *, missing: bool = False, status: str | None = None
) -> None:
    """Deterministic process-table answers for the anchor-liveness classification."""

    def process(pid: int) -> SimpleNamespace:
        if missing:
            raise psutil.NoSuchProcess(pid)
        return SimpleNamespace(status=lambda: status or psutil.STATUS_RUNNING)

    monkeypatch.setattr(
        store,
        "psutil",
        SimpleNamespace(
            Process=process,
            NoSuchProcess=psutil.NoSuchProcess,
            AccessDenied=psutil.AccessDenied,
            STATUS_ZOMBIE=psutil.STATUS_ZOMBIE,
            STATUS_DEAD=psutil.STATUS_DEAD,
        ),
    )
    _stub_birth(monkeypatch, 998.0)


def test_attested_caller_passes() -> None:
    lease = _lease(recorded_tree())
    store.verify_caller(lease, attested_caller(lease))


def test_chain_mismatch_when_anchor_is_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_liveness(monkeypatch)
    with pytest.raises(store.ImpersonationError, match="chain-mismatch"):
        store.verify_caller(_lease(recorded_tree()), unrelated_caller())


def test_anchor_dead_when_the_process_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_liveness(monkeypatch, missing=True)
    with pytest.raises(store.ImpersonationError, match="anchor-dead"):
        store.verify_caller(_lease(recorded_tree()), unrelated_caller())


def test_anchor_dead_when_the_pid_was_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_liveness(monkeypatch)
    _stub_birth(monkeypatch, 5000.0)
    with pytest.raises(store.ImpersonationError, match="pid now belongs"):
        store.verify_caller(_lease(recorded_tree()), unrelated_caller())


def test_a_zombie_anchor_is_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_liveness(monkeypatch, status=psutil.STATUS_ZOMBIE)
    with pytest.raises(store.ImpersonationError, match="anchor-dead"):
        store.verify_caller(_lease(recorded_tree()), unrelated_caller())


def test_no_anchor_records_fail_closed() -> None:
    tree: dict[str, Any] = {
        "pid": 4242,
        "name": "python3.12",
        "executable": "/usr/bin/python3.12",
        "created_at": 1000.0,
        "parent_pid": 1,
        "ancestors": [],
    }
    with pytest.raises(store.ImpersonationError, match="no-anchor"):
        store.verify_caller(_lease(tree), unrelated_caller())


def _native_claude_tree() -> dict[str, Any]:
    """The observed claude native layout: name=version, versions/<version> exe."""
    return {
        "pid": 4242,
        "name": "python3.12",
        "executable": "/usr/bin/python3.12",
        "created_at": 1000.0,
        "parent_pid": 4241,
        "ancestors": [
            {
                "pid": 4241,
                "name": "zsh",
                "executable": "/bin/zsh",
                "created_at": 999.0,
                "parent_pid": 4240,
            },
            {
                "pid": 4240,
                "name": "2.1.274",
                "executable": "/Users/dev/.local/share/claude/versions/2.1.274",
                "created_at": 998.0,
                "parent_pid": 1,
            },
        ],
    }


def test_claude_native_layout_attests() -> None:
    """A native claude controller (name=version) is a recorded anchor."""
    lease = _lease(_native_claude_tree())
    store.verify_caller(lease, attested_caller(lease))


def _anchor_recognized(node: dict[str, Any]) -> bool:
    """Behavioral probe: a lone node is an anchor iff a descending caller attests."""
    tree: dict[str, Any] = {
        "pid": 4242,
        "name": "python3.12",
        "executable": "/usr/bin/python3.12",
        "created_at": 1000.0,
        "parent_pid": int(node["pid"]),
        "ancestors": [node],
    }
    lease = _lease(tree)
    try:
        store.verify_caller(lease, attested_caller(lease))
    except store.ImpersonationError:
        return False
    return True


@pytest.mark.parametrize(
    ("name", "executable", "recognized"),
    [
        ("codex", "/opt/codex", True),
        ("codex", "/usr/local/bin/codex", True),
        ("claude", "/usr/local/bin/claude", True),
        ("2.1.269", "/home/u/.local/share/claude/versions/2.1.269", True),
        ("2.1.274", "/Users/u/.local/share/claude/versions/2.1.274", True),
        ("3.2.1", "/opt/claude/versions/3.2.1", True),
        ("python3.12", "/usr/bin/python3.12", False),
        ("2.1.274", "/opt/2.1.274", False),
        ("2.1.274", "/opt/other/versions/2.1.274", False),
        ("2.1.274", "/opt/other/claude/2.1.274", False),
        ("2.1.274", "/opt/other/claude/versions/nightly", False),
        ("2.1.274", "/opt/other/claude/versions/2.1.274/extra", False),
    ],
)
def test_provider_anchor_recognition(name: str, executable: str, recognized: bool) -> None:
    node = {
        "pid": 4240,
        "name": name,
        "executable": executable,
        "created_at": 998.0,
        "parent_pid": 1,
    }
    assert _anchor_recognized(node) is recognized


@pytest.mark.parametrize(
    ("name", "script", "recognized"),
    [
        ("node", "/opt/homebrew/bin/dsh", True),
        ("node", "/Users/u/.npm/_npx/0a1b/node_modules/.bin/dsh", True),
        ("node", "/usr/lib/node_modules/@deepseek-ai/dsh/lib/bin.js", True),
        ("node", "/srv/app/server.js", False),
        ("node", None, False),
        ("python3.12", "/opt/homebrew/bin/dsh", False),
    ],
)
def test_dsh_node_anchor_recognition(name: str, script: str | None, recognized: bool) -> None:
    """DeepSeek Harness runs as ``node <dsh launcher>``: only the recorded script names it."""
    node: dict[str, Any] = {
        "pid": 4240,
        "name": name,
        "executable": f"/opt/homebrew/bin/{name}",
        "created_at": 998.0,
        "parent_pid": 1,
    }
    if script is not None:
        node["script"] = script
    assert _anchor_recognized(node) is recognized


def test_same_pid_with_a_drifted_start_time_does_not_attest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_liveness(monkeypatch)
    lease = _lease(recorded_tree())
    caller = attested_caller(lease)
    for node in caller["ancestors"]:
        if node["pid"] == 4240:
            node["created_at"] = 998.0 + 60.0
    with pytest.raises(store.ImpersonationError, match="chain-mismatch"):
        store.verify_caller(lease, caller)


def _agent(db_conn: Any) -> RuntimeIncarnation:
    agent_id = create_agent(db_conn)
    owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (agent_id, machine_name(), owner.generation, owner.owner),
    )
    db_conn.commit()
    return owner


def _active(owner: RuntimeIncarnation, tree: dict[str, Any]) -> dict[str, Any]:
    lease = leases.request(
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex"),
        ttl_seconds=300,
        reason="Attestation coverage",
        process_metadata=tree,
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
    )
    leases.accept(lease["id"], owner.agent_id, owner, "Handoff brief")
    leases.activate(lease["id"], owner)
    return lease


def test_generation_crossing_is_refused(db_conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller attested for one generation cannot drive another's session id."""
    first = _active(_agent(db_conn), recorded_tree())
    second_tree = recorded_tree()
    second_tree["ancestors"] = [
        {
            "pid": 4341,
            "name": "zsh",
            "executable": "/bin/zsh",
            "created_at": 1999.0,
            "parent_pid": 4340,
        },
        {
            "pid": 4340,
            "name": "codex",
            "executable": "/opt/codex",
            "created_at": 1998.0,
            "parent_pid": 1,
        },
    ]
    second = _active(_agent(db_conn), second_tree)
    _stub_liveness(monkeypatch)
    _stub_birth(monkeypatch, 1998.0)
    with pytest.raises(store.ImpersonationError, match="chain-mismatch"):
        leases.require_active(second["id"], attested_caller(first))


def test_terminal_sessions_report_stale_without_attestation(db_conn: Any) -> None:
    """A terminal lease is classified before any anchor work: native/TTL recovery
    never depends on the dead controller tree (the second, incarnation-held gate)."""
    owner = _agent(db_conn)
    lease = _active(owner, recorded_tree())
    leases.release(lease["id"], attested_caller(lease), "Done")
    with pytest.raises(leases.ImpersonationError, match="stale-session"):
        leases.require_active(lease["id"], unrelated_caller())


def test_dsh_request_mints_a_session_relay_credential(db_conn: Any) -> None:
    """dsh runs its relay in the controller session, like claude: the request
    mints the scoped credential, and a thread id or codex remote is refused."""
    owner = _agent(db_conn)
    caller = CallerIdentity(kind="external_agent", subject="dsh")
    with pytest.raises(ValueError, match="dsh relay routes to its owner"):
        leases.request(
            owner.agent_id,
            caller=caller,
            reason="dsh",
            relay_provider="dsh",
            relay_thread_id="thread",
        )
    lease = leases.request(
        owner.agent_id,
        caller=caller,
        ttl_seconds=300,
        reason="dsh",
        process_metadata=recorded_tree(),
        relay_provider="dsh",
    )
    assert lease["relay_provider"] == "dsh"
    assert lease["relay_token"]

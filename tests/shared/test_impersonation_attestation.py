"""Caller attestation on the controller surface: tiers, pid reuse, generation crossing."""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import psutil
import pytest

from shared.agents import impersonation as leases
from shared.agents.impersonation import _impersonation_store as store
from shared.caller_identity import CallerIdentity
from shared.db import create_agent
from shared.machine import machine_name
from shared.native_process import ownership
from shared.runtime_incarnation import RuntimeIncarnation
from tests.impersonation_support import (
    attested_caller,
    native_identity,
    recorded_tree,
    unrelated_caller,
)


def _running_process(pid: int) -> SimpleNamespace:
    return SimpleNamespace(pid=pid, create_time=lambda: 101.0, status=lambda: psutil.STATUS_RUNNING)


def test_linux_tick_identity_survives_wall_clock_shift(monkeypatch: pytest.MonkeyPatch) -> None:
    boot = "33f1e236-5b3d-48d8-a002-e117e3b95fc4"
    monkeypatch.setattr(ownership, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(store, "sys", SimpleNamespace(platform="linux"), raising=False)
    monkeypatch.setattr(store, "native_boot_id", lambda: boot, raising=False)
    monkeypatch.setattr(ownership, "pid_starttime_ticks", Mock(return_value=500))
    monkeypatch.setattr(
        store.psutil,
        "Process",
        _running_process,
    )
    anchor = {"pid": 42, "name": "codex", "created_at": 100.0, "starttime": 500, "boot_id": boot}
    caller = {**anchor, "created_at": 101.0}
    assert store.classify_anchor(anchor) == "alive"
    store.verify_caller(_lease(anchor), caller)


def test_linux_missing_ticks_are_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "sys", SimpleNamespace(platform="linux"), raising=False)
    monkeypatch.setattr(ownership, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(
        store.psutil,
        "Process",
        _running_process,
    )
    assert store.classify_anchor({"pid": 42, "created_at": 100.0}) == "unknown"


@pytest.mark.parametrize(
    "field,value",
    [
        ("pid", True),
        ("pid", -1),
        ("created_at", float("nan")),
        ("created_at", 10**1000),
        ("created_at", 0),
        ("starttime", None),
        ("starttime", False),
        ("starttime", -1),
        ("starttime", "500"),
        ("boot_id", None),
        ("boot_id", "garbage"),
    ],
)
def test_invalid_linux_anchor_cannot_attest_or_prove_death(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    monkeypatch.setattr(store, "sys", SimpleNamespace(platform="linux"))
    anchor = {
        "pid": 42,
        "name": "codex",
        "created_at": 100.0,
        "starttime": 500,
        "boot_id": str(uuid4()),
        field: value,
    }
    assert store.classify_anchor(anchor) == "unknown"
    assert not store._same_process(anchor, dict(anchor))
    with pytest.raises(store.ImpersonationError, match="anchor-unavailable"):
        store.verify_caller(_lease(anchor), dict(anchor))


def test_native_metadata_producer_survives_linux_wall_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boot = str(uuid4())
    shift = 0.0

    class Process:
        def __init__(self, pid: int = 42) -> None:
            self.pid = pid

        def create_time(self) -> float:
            return 100.0 + self.pid + shift

        def name(self) -> str:
            return "codex" if self.pid == 41 else "python"

        def exe(self) -> str:
            return "/opt/" + self.name()

        def ppid(self) -> int:
            return 41 if self.pid == 42 else 1

        def parent(self) -> "Process | None":
            return Process(41) if self.pid == 42 else None

        def status(self) -> str:
            return psutil.STATUS_RUNNING

    monkeypatch.setattr(store, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(ownership, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(store, "native_boot_id", lambda: boot)
    monkeypatch.setattr(ownership, "native_boot_id", lambda: boot)

    def ticks(pid: int) -> int:
        return 500 + pid

    monkeypatch.setattr(ownership, "pid_starttime_ticks", ticks)
    monkeypatch.setattr(ownership.psutil, "Process", Process)
    recorded = ownership.process_metadata()
    original = deepcopy(recorded)
    shift = 1.0
    caller = ownership.process_metadata()
    for node in [recorded, *recorded["ancestors"]]:
        assert node["starttime"] == 500 + node["pid"]
        assert node["boot_id"] == boot
    store.verify_caller(_lease(recorded), caller)
    assert store.provider_anchor_states(recorded) == ["alive"]
    assert recorded == original
    monkeypatch.setattr(ownership, "pid_starttime_ticks", Mock(return_value=None))
    monkeypatch.setattr(ownership.psutil, "pid_exists", Mock(return_value=True))
    assert store.provider_anchor_states(recorded) == ["unknown"]
    monkeypatch.setattr(ownership, "pid_starttime_ticks", Mock(return_value=0))
    assert store.provider_anchor_states(recorded) == ["unknown"]


def test_reboot_never_reattests_old_pid_and_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(ownership, "sys", SimpleNamespace(platform="linux"))
    boot, current_boot = str(uuid4()), str(uuid4())
    anchor = {"pid": 42, "name": "codex", "created_at": 100.0, "starttime": 500, "boot_id": boot}
    monkeypatch.setattr(store, "native_boot_id", lambda: current_boot)
    assert not store._same_process(anchor, dict(anchor))
    assert not store._same_process(anchor, {**anchor, "boot_id": current_boot})
    assert store.classify_anchor(anchor) == "dead"


@pytest.mark.parametrize("boot", [None, "", "unreadable"])
def test_unavailable_current_boot_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    boot: str | None,
) -> None:
    anchor = recorded_tree()["ancestors"][-1]
    monkeypatch.setattr(store, "sys", SimpleNamespace(platform="linux"))
    anchor["starttime"] = 500
    monkeypatch.setattr(store, "native_boot_id", lambda: boot)
    assert store.classify_anchor(anchor) == "unknown"
    assert not store._same_process(anchor, dict(anchor))


@pytest.mark.parametrize("missing", ["boot_id", "starttime"])
def test_windows_head_requires_explicit_native_fields(
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    monkeypatch.setattr(store, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(ownership, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(store, "native_boot_id", lambda: None)
    head = {"pid": 42, "name": "codex", "created_at": 100.0, "starttime": None, "boot_id": None}
    store.verify_caller(_lease(head), dict(head))
    del head[missing]
    with pytest.raises(store.ImpersonationError, match="anchor-unavailable"):
        store.verify_caller(_lease(head), dict(head))


@pytest.mark.parametrize(
    "error,state", [(psutil.AccessDenied(4240), "denied"), (OSError("unreadable"), "unknown")]
)
def test_anchor_read_errors_are_not_death(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    state: str,
) -> None:
    monkeypatch.setattr(store.psutil, "Process", Mock(side_effect=error))
    tree = recorded_tree()
    assert store.provider_anchor_states(tree) == [state]
    with pytest.raises(store.ImpersonationError, match="anchor-unavailable"):
        store.verify_caller(_lease(tree), unrelated_caller())


def test_attested_native_identity_still_requires_same_machine() -> None:
    lease = _lease(recorded_tree())
    lease["machine"] += "-other"
    with pytest.raises(store.ImpersonationError, match="own machine"):
        store.authenticate(lease, attested_caller(lease))


def _stub_birth(monkeypatch: pytest.MonkeyPatch, birth: float) -> None:
    monkeypatch.setattr(ownership, "stable_create_time", Mock(return_value=birth))
    monkeypatch.setattr(
        ownership, "pid_starttime_ticks", Mock(return_value=native_identity(birth)["starttime"])
    )


def _lease(tree: dict[str, Any]) -> dict[str, Any]:
    return {"process_metadata": tree, "machine": machine_name()}


def _stub_liveness(
    monkeypatch: pytest.MonkeyPatch, *, missing: bool = False, status: str | None = None
) -> None:
    """Deterministic process-table answers for the anchor-liveness classification."""

    def process(pid: int) -> SimpleNamespace:
        if missing:
            raise psutil.NoSuchProcess(pid)
        return SimpleNamespace(pid=pid, status=lambda: status or psutil.STATUS_RUNNING)

    monkeypatch.setattr(
        store.psutil,
        "Process",
        process,
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
        **native_identity(1000.0),
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
        **native_identity(1000.0),
        "parent_pid": 4241,
        "ancestors": [
            {
                "pid": 4241,
                "name": "zsh",
                "executable": "/bin/zsh",
                **native_identity(999.0),
                "parent_pid": 4240,
            },
            {
                "pid": 4240,
                "name": "2.1.274",
                "executable": "/Users/dev/.local/share/claude/versions/2.1.274",
                **native_identity(998.0),
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
        **native_identity(1000.0),
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
        **native_identity(998.0),
        "parent_pid": 1,
    }
    assert _anchor_recognized(node) is recognized


def test_same_pid_with_a_different_native_birth_does_not_attest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_liveness(monkeypatch)
    lease = _lease(recorded_tree())
    caller = attested_caller(lease)
    for node in caller["ancestors"]:
        if node["pid"] == 4240:
            node.update(native_identity(998.0 + 60.0))
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
            **native_identity(1999.0),
            "parent_pid": 4340,
        },
        {
            "pid": 4340,
            "name": "codex",
            "executable": "/opt/codex",
            **native_identity(1998.0),
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

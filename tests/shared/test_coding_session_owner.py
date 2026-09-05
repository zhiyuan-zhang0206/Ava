"""Generation and cleanup contracts for canonical coding-session ownership."""

from __future__ import annotations

import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest

from shared import coding_session_owner as owner
from shared.config import settings

NOW = dt.datetime(2026, 9, 2, 0, 0, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def _isolated_host_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.general, "cluster_registry", tmp_path / "host" / "clusters.json")


def _key(tmp_path: Path, workspace: str = "workspace") -> owner.CodingSessionKey:
    path = tmp_path / workspace
    path.mkdir(parents=True, exist_ok=True)
    return owner.canonical_key(path, tool="codex", cluster=tmp_path / "cluster")


def _claim(
    key: owner.CodingSessionKey,
    *,
    agent_id: int,
    now: dt.datetime = NOW,
    live: set[str] | None = None,
    terminated_generation: str | None = None,
    stopped: list[str] | None = None,
) -> owner.CodingSessionClaim:
    live_names: set[str] = live if live is not None else set()
    stopped_names = stopped if stopped is not None else []

    def _list_sessions() -> list[str]:
        return sorted(live_names)

    def _is_live(name: str) -> bool:
        return name in live_names

    def _stop(name: str) -> bool:
        stopped_names.append(name)
        live_names.discard(name)
        return True

    workspace = Path(key.workspace)
    return owner.claim(
        key,
        owner_agent_id=agent_id,
        tasks_file=workspace / "tasks.md",
        work_file=workspace / "work.md",
        ttl_seconds=3600,
        now=now,
        list_sessions=_list_sessions,
        session_live=_is_live,
        terminate_session=_stop,
        terminated_generation=terminated_generation,
    )


def _publish(claim: owner.CodingSessionClaim, session_id: int) -> owner.CodingSessionOwner:
    record = claim.owner
    assert record.generation is not None and record.expected_suffix is not None
    assert record.owner_agent_id is not None
    generation = record.generation
    expected_suffix = record.expected_suffix
    owner_agent_id = record.owner_agent_id
    supervisor_id = session_id + 100
    record = owner.attach_supervisor(
        record.key,
        generation,
        session_id=supervisor_id,
        session_name=owner.full_session_name(
            owner_agent_id,
            supervisor_id,
            owner.supervisor_suffix(record.key, generation),
        ),
    )
    return owner.publish_active(
        record.key,
        generation,
        session_id=session_id,
        session_name=owner.full_session_name(owner_agent_id, session_id, expected_suffix),
    )


def test_concurrent_claims_never_lose_the_winner(tmp_path: Path) -> None:
    for round_number in range(5):
        key = _key(tmp_path, f"workspace-{round_number}")

        def _attempt(
            agent_id: int,
            claim_key: owner.CodingSessionKey = key,
        ) -> owner.CodingSessionClaim:
            return _claim(claim_key, agent_id=agent_id)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_attempt, range(41, 49)))

        launched = [result for result in results if result.action == "launch"]
        assert len(launched) == 1
        assert all(result.action in {"launch", "busy", "adopt"} for result in results)
        current = owner.read(key)
        assert current.status in {"launching", "active"}
        assert current.generation == launched[0].owner.generation


def test_stale_launching_with_live_session_is_busy_not_replaced(tmp_path: Path) -> None:
    key = _key(tmp_path)
    first = _claim(key, agent_id=41)
    record = first.owner
    assert record.expected_suffix is not None and record.state_dir is not None
    partial_name = owner.full_session_name(41, 3, record.expected_suffix)
    record.state_dir.mkdir(parents=True)
    (record.state_dir / "state_5.sqlite").write_text("partial")
    live = {partial_name}
    stopped: list[str] = []

    second = _claim(
        key,
        agent_id=42,
        now=NOW + dt.timedelta(seconds=61),
        live=live,
        stopped=stopped,
    )

    assert second.action == "busy"
    assert second.owner == record
    assert owner.read(key) == record
    assert stopped == []
    assert live == {partial_name}
    assert record.state_dir.exists()


def test_stale_launching_without_live_session_is_replaced(tmp_path: Path) -> None:
    key = _key(tmp_path)
    first = _claim(key, agent_id=41)
    record = first.owner
    assert record.state_dir is not None
    record.state_dir.mkdir(parents=True)
    (record.state_dir / "state_5.sqlite").write_text("partial")
    stopped: list[str] = []

    replacement = _claim(
        key,
        agent_id=42,
        now=NOW + dt.timedelta(seconds=61),
        stopped=stopped,
    )

    assert replacement.action == "launch"
    assert replacement.owner.generation != record.generation
    assert owner.read(key) == replacement.owner
    assert stopped == []
    assert not record.state_dir.exists()


def test_publish_active_from_live_stale_generation_succeeds(tmp_path: Path) -> None:
    key = _key(tmp_path)
    first = _claim(key, agent_id=41)
    record = first.owner
    assert record.expected_suffix is not None
    partial_name = owner.full_session_name(41, 3, record.expected_suffix)

    second = _claim(
        key,
        agent_id=42,
        now=NOW + dt.timedelta(seconds=61),
        live={partial_name},
    )
    active = _publish(first, 3)

    assert second.action == "busy"
    assert active.status == "active"
    assert active.generation == record.generation
    assert owner.read(key) == active


def test_live_generation_is_adopted_across_agents(tmp_path: Path) -> None:
    key = _key(tmp_path)
    first = _claim(key, agent_id=41)
    active = _publish(first, 3)
    assert active.session_name is not None and active.supervisor_session_name is not None
    live = {active.session_name, active.supervisor_session_name}

    adopted = _claim(key, agent_id=99, live=live)

    assert adopted.action == "adopt"
    assert adopted.owner.generation == active.generation
    assert adopted.owner.owner_agent_id == 41
    assert adopted.owner.session_id == 3


def test_terminated_owner_transfers_after_exact_cleanup(tmp_path: Path) -> None:
    key = _key(tmp_path)
    first = _claim(key, agent_id=41)
    active = _publish(first, 3)
    assert active.session_name is not None
    assert active.supervisor_session_name is not None
    assert active.state_dir is not None
    active.state_dir.mkdir(parents=True)
    stale_sqlite = active.state_dir / "state_5.sqlite"
    stale_sqlite.write_text("old")
    live = {active.session_name, active.supervisor_session_name}
    stopped: list[str] = []

    replacement = _claim(
        key,
        agent_id=99,
        live=live,
        stopped=stopped,
        terminated_generation=active.generation,
    )

    assert replacement.action == "launch"
    assert replacement.owner.generation != active.generation
    assert replacement.owner.owner_agent_id == 99
    assert stopped == [active.session_name]
    assert not stale_sqlite.exists()


def test_expired_generation_is_reclaimed_before_rebuild(tmp_path: Path) -> None:
    key = _key(tmp_path)
    first = _claim(key, agent_id=41)
    active = _publish(first, 0)
    assert active.session_name is not None and active.state_dir is not None
    assert active.supervisor_session_name is not None
    active.state_dir.mkdir(parents=True)
    (active.state_dir / "history.jsonl").write_text("mutable")
    live = {active.session_name, active.supervisor_session_name}

    replacement = _claim(
        key,
        agent_id=42,
        now=NOW + dt.timedelta(hours=2),
        live=live,
    )

    assert replacement.action == "launch"
    assert replacement.owner.generation != active.generation
    assert not active.state_dir.exists()
    assert live == {active.supervisor_session_name}


def test_terminal_cleanup_is_generation_scoped_and_removes_state(tmp_path: Path) -> None:
    key = _key(tmp_path)
    first = _claim(key, agent_id=41)
    active = _publish(first, 0)
    assert active.generation is not None
    assert active.session_name is not None and active.state_dir is not None
    assert active.supervisor_session_name is not None
    active.state_dir.mkdir(parents=True)
    (active.state_dir / "state_5.sqlite").write_text("mutable")
    live = {active.session_name, active.supervisor_session_name}
    stopped: list[str] = []

    def _list_sessions() -> list[str]:
        return sorted(live)

    def _is_live(name: str) -> bool:
        return name in live

    def _unexpected_stop(_name: str) -> bool:
        raise AssertionError("stale generation must not stop a session")

    def _stop(name: str) -> bool:
        stopped.append(name)
        live.discard(name)
        return True

    assert not owner.terminate_generation(
        key,
        "stale-generation",
        reason="explicit-cancel",
        list_sessions=_list_sessions,
        session_live=_is_live,
        terminate_session=_unexpected_stop,
    )
    assert owner.terminate_generation(
        key,
        active.generation,
        reason="explicit-cancel",
        now=NOW + dt.timedelta(minutes=1),
        list_sessions=_list_sessions,
        session_live=_is_live,
        terminate_session=_stop,
    )

    terminal = owner.read(key)
    assert terminal.status == "terminal"
    assert terminal.terminal_reason == "explicit-cancel"
    assert stopped == [active.session_name]
    assert not active.state_dir.exists()


def test_different_workspaces_have_distinct_records_and_state(tmp_path: Path) -> None:
    first = _claim(_key(tmp_path, "same-name-a/work"), agent_id=41)
    second = _claim(_key(tmp_path, "same-name-b/work"), agent_id=42)

    assert first.owner.key.workspace != second.owner.key.workspace
    assert owner.state_path(first.owner.key) != owner.state_path(second.owner.key)
    assert first.owner.state_dir != second.owner.state_dir
    assert first.owner.state_dir is not None and second.owner.state_dir is not None
    assert first.owner.state_dir / "state_5.sqlite" != second.owner.state_dir / "state_5.sqlite"
    assert first.owner.state_dir / "log" != second.owner.state_dir / "log"


def test_same_workspace_in_another_cluster_is_invisible(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first_key = owner.canonical_key(workspace, tool="codex", cluster=tmp_path / "cluster-a")
    second_key = owner.canonical_key(workspace, tool="codex", cluster=tmp_path / "cluster-b")

    _claim(first_key, agent_id=41)

    assert owner.state_path(first_key) != owner.state_path(second_key)
    assert owner.read(second_key).status == "inactive"


def test_terminal_rebuild_publishes_new_generation_and_handle(tmp_path: Path) -> None:
    key = _key(tmp_path)
    first_claim = _claim(key, agent_id=41)
    first = _publish(first_claim, 0)
    assert first.generation is not None and first.session_name is not None
    assert first.supervisor_session_name is not None
    live = {first.session_name, first.supervisor_session_name}

    def _list_sessions() -> list[str]:
        return sorted(live)

    def _is_live(name: str) -> bool:
        return name in live

    def _stop(name: str) -> bool:
        live.discard(name)
        return True

    assert owner.terminate_generation(
        key,
        first.generation,
        reason="collaboration-handoff",
        list_sessions=_list_sessions,
        session_live=_is_live,
        terminate_session=_stop,
    )
    second_claim = _claim(key, agent_id=42, now=NOW + dt.timedelta(minutes=1))
    second = _publish(second_claim, 7)

    assert second.generation != first.generation
    assert second.session_id == 7
    assert second.session_name != first.session_name
    assert owner.read(key) == second


def test_corrupt_owner_fails_closed(tmp_path: Path) -> None:
    key = _key(tmp_path)
    path = owner.state_path(key)
    path.write_text("{broken")

    with pytest.raises(owner.InvalidCodingSessionOwnerError):
        _claim(key, agent_id=41)


def test_misdirected_full_handle_fails_closed(tmp_path: Path) -> None:
    key = _key(tmp_path)
    active = _publish(_claim(key, agent_id=41), 3)
    assert active.session_name is not None
    path = owner.state_path(key)
    payload = cast("dict[str, object]", json.loads(path.read_text()))
    payload["session_name"] = "ava-gateway"
    path.write_text(json.dumps(payload))

    invalid = owner.read(key)
    assert invalid.status == "invalid"
    assert "session_name does not match" in (invalid.error or "")
    with pytest.raises(owner.InvalidCodingSessionOwnerError):
        _claim(key, agent_id=99, live={active.session_name})

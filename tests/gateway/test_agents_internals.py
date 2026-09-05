"""Unit tests for gateway/agents.py (gateway impl, originally split from shared/agents.py ↦ gateway/).

`_launch_agent_process` launching a real process would pollute the dev machine and slow tests;
most tests monkeypatch it and only verify DB side-effects (INSERT agents / agents +
status transitions + lifecycle inbound notifications for resurrect / respawn).

`TestLaunchConfirm` is the exception: it does not replace `_launch_agent_process` wholesale,
but only fakes the internal `subprocess.run` so that the spawn returncode=0 without actually starting
a child python — simulating the production failure mode of "detach succeeded but child crashed"
(agent 137 / agent 44 incident).

spawn does not insert inbound; resurrect inserts kind='resurrect' source=resurrected_by;
respawn inserts kind='restart_completed' source=the source of the original 'restart' inbound.
Once the new process starts, the claim side dispatches these lifecycle inbounds into
lifecycle markers appended to messages, letting the agent know what transition just happened.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

import shared.db
from gateway.app import app
from ops import agent_launch
from ops.agent_launch import (
    _confirm_launch_or_force_terminated,
    _launch_or_force_terminated,
    _wait_for_agent_claim,
)
from ops.agent_wake import (
    AutoResurrectClaim,
    ResurrectClaimStaleError,
    ResurrectTriggerStaleError,
)
from ops.agents import (
    create_agent_row,
    respawn_agent,
    resurrect_agent,
)
from ops.ops_lifecycle import _force_mark_terminated
from shared import boot_timing
from shared.agent_snapshot import select_one
from shared.agents import (
    AgentNotFound,
    AgentStatus,
    ForkCheckpointNotFound,
    MachinePaused,
    ResurrectAlreadyAlive,
    ResurrectError,
    TerminationSource,
)
from shared.config import settings
from shared.env_registry import AGENT_CONFIG_OVERLAY_ENV
from shared.envelope import wrap_inbound
from shared.machine import machine_name

# The autouse `_guard_agent_launch` (tests/conftest.py) stubs _launch_agent_process
# for every test not marked `real_agent_launch`, so the vast majority here only
# assert DB side-effects — deterministic and parallel-safe. Only the tests that
# run the real confirm-timeout poll loop (`@pytest.mark.real_agent_launch` with a
# child that never claims) or assert on real elapsed wall-clock keep
# `@pytest.mark.flaky` to run serial.


def _test_pool() -> ConnectionPool:
    """Return a concretely typed pool for helpers that open their own pool."""
    return cast(
        ConnectionPool,
        ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2),
    )


def _record_agent_ids(target: list[int]) -> Callable[..., None]:
    def _record(agent_id: int, **_kwargs: object) -> None:
        target.append(agent_id)

    return _record


def _noop(*_args: object, **_kwargs: object) -> None:
    return None


def _agents_row(db: psycopg.Connection, agent_id: int) -> tuple[int, str, str, int | None] | None:
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, spawner, status, pid FROM agents_meta WHERE id = %s",
            (agent_id,),
        )
        return cur.fetchone()


def _agents_pid_started_at(db: psycopg.Connection, agent_id: int) -> tuple[int | None, object]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT pid, started_at FROM agents_meta WHERE id = %s",
            (agent_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return row[0], row[1]


def _inbound_count(db: psycopg.Connection, agent_id: int) -> int:
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM inbound_messages WHERE agent_id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _terminated_with_chat(db: psycopg.Connection, agent_id: int, content: str) -> int:
    with db.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (agent_id,))
    db.commit()
    return shared.db.insert_inbound_message(db, agent_id, content, source="user")


def _terminated_on_machine_with_chat(
    db: psycopg.Connection, agent_id: int, machine: str, content: str
) -> int:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO machines (name, role) VALUES (%s, ARRAY['agent-runner'])",
            (machine,),
        )
        cur.execute(
            "UPDATE agents_meta SET machine=%s, status='terminated' WHERE id=%s",
            (machine, agent_id),
        )
    db.commit()
    return shared.db.insert_inbound_message(db, agent_id, content, source="user")


def _pause_and_force_sweep(machine: str) -> None:
    from gateway.routers._machine_pause import _list_agent_rows_for_pause_blocking
    from ops.ops_lifecycle import _force_terminate_transaction
    from shared import machines

    machines.pause(machine, reason="test pause race")
    with _test_pool() as pool:
        for agent_id, _was_live in _list_agent_rows_for_pause_blocking(pool, machine):
            _force_terminate_transaction(agent_id, pool, source="machine-pause", kill_process=True)


def test_machine_pause_resolves_old_and_new_fingerprint_alerts(
    db_conn: psycopg.Connection,
) -> None:
    """Expected absence closes every open episode across fingerprint conventions."""
    from datetime import UTC, datetime

    from psycopg.types.json import Jsonb

    from gateway.routers._machine_pause import _resolve_machine_alerts_blocking
    from shared.alerts import fingerprint

    identity_labels = {"alertname": "machine offline", "machine": "away"}
    old_labels = {**identity_labels, "severity": "warning"}
    new_labels = {**identity_labels, "severity": "error"}
    old_start = datetime(2026, 8, 5, tzinfo=UTC)
    new_start = datetime(2026, 8, 26, tzinfo=UTC)
    with db_conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO alerts (status, severity, alertname, labels, annotations, "
            "starts_at, fingerprint, source, notified_at) VALUES "
            "('unresolved', %s, 'machine offline', %s, '{}', %s, %s, "
            "'machine-probe', now())",
            [
                ("warning", Jsonb(old_labels), old_start, fingerprint(old_labels)),
                ("error", Jsonb(new_labels), new_start, fingerprint(identity_labels)),
            ],
        )
    db_conn.commit()

    with _test_pool() as pool:
        _resolve_machine_alerts_blocking(pool, "away")

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT status, severity, labels->>'severity', fingerprint, ends_at "
            "FROM alerts WHERE labels->>'machine' = 'away' ORDER BY starts_at"
        )
        rows = cur.fetchall()
    assert [(row[0], row[1], row[2], row[3]) for row in rows] == [
        ("resolved", "warning", "warning", fingerprint(old_labels)),
        ("resolved", "error", "error", fingerprint(identity_labels)),
    ]
    assert all(row[4] is not None for row in rows)


def _termination_row(db: psycopg.Connection, agent_id: int) -> tuple[str, str | None, bool]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT status, termination_source, "
            "last_force_terminate_inbound_id IS NOT NULL "
            "FROM agents_meta WHERE id=%s",
            (agent_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return row


def _resurrect_inbound_count(db: psycopg.Connection, agent_id: int) -> int:
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='resurrect'",
            (agent_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _spawn_agent(
    *,
    spawner: str = "user",
    fork_from: int | None = None,
    fork_checkpoint: str | None = None,
    config: dict[str, object] | None = None,
    label: str | None = None,
    prompt: str | None = None,
    prompt_source: str | None = None,
) -> int:
    """Test setup helper — mirrors the pre-#1236 `spawn_agent()` contract
    (create row + launch) as the two-phase split: `create_agent_row`
    (gateway-side, the main data-plane identity) then `_launch_agent_process`
    (runner-side), with the launch stubbed by the autouse guard. The launch op's
    prompt-delivery half is covered in tests/gateway/test_operations.py."""
    agent_id, birth_config = create_agent_row(
        spawner=spawner,
        fork_from=fork_from,
        fork_checkpoint=fork_checkpoint,
        machine=machine_name(),
        config=config,
        label=label,
        prompt=prompt,
        prompt_source=prompt_source,
    )
    # Same call shape as the old spawn_agent (and launch_agent_op's launch):
    # config_overlay keyword, birth_config kwarg, confirm=False (the child claims
    # via the DB, off this path).
    agent_launch._launch_agent_process(
        agent_id, config_overlay=config, birth_config=birth_config, confirm=False
    )
    return agent_id


def _inbound_rows(db: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str | None]]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT content, kind, source FROM inbound_messages "
            "WHERE agent_id = %s ORDER BY id ASC",
            (agent_id,),
        )
        return cur.fetchall()


class TestSpawnAgent:
    def test_inserts_thread_agent_and_starts_session(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The create+launch split (create_agent_row + _launch_agent_process) inserts
        agents + agents_meta row and does **not** insert inbound (asymmetric with
        resurrect — spawn creates from nothing, so no notification needed)."""
        launched: list[int] = []

        def _record(agent_id: int, **_kw: object) -> None:
            launched.append(agent_id)

        monkeypatch.setattr("ops.agent_launch._launch_agent_process", _record)

        new_id = _spawn_agent()

        assert launched == [new_id]
        # agents row: status='idling', spawner='user' (default), pid not yet filled
        assert _agents_row(db_conn, new_id) == (new_id, "user", "idling", None)
        # spawn should not insert inbound
        assert _inbound_count(db_conn, new_id) == 0

    def test_spawner_recorded(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The birth-time lineage string is stamped into both metadata fields."""
        parent_id = _spawn_agent()  # spawner='user' default
        child_id = _spawn_agent(spawner=f"agent:{parent_id}")

        with db_conn.cursor() as cur:
            cur.execute("SELECT spawner, born_spawner FROM agents_meta WHERE id = %s", (child_id,))
            row = cur.fetchone()
        assert row is not None
        assert row == (f"agent:{parent_id}", f"agent:{parent_id}")

    def test_label_stored_sticky(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A spawner-assigned label is stored with label_user_set=TRUE so the
        labeler's CAS (WHERE label IS NULL AND NOT label_user_set) skips it."""
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        new_id = _spawn_agent(label="auth worker")
        with db_conn.cursor() as cur:
            cur.execute("SELECT label, label_user_set FROM agents WHERE id=%s", (new_id,))
            row = cur.fetchone()
        assert row == ("auth worker", True)

        # Default (no label) stays NULL + not-set so the labeler can fill it.
        plain_id = _spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute("SELECT label, label_user_set FROM agents WHERE id=%s", (plain_id,))
            assert cur.fetchone() == (None, False)

    def test_session_failure_leaves_unclaimed_idling_row(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the launch fails, the create+launch helper does **not** clean the DB — that is the job of monitoring / weekly cleanup tasks."""

        def boom(_id: int, **_kw: object) -> None:
            raise RuntimeError("launch \u6545\u610f\u6302")

        monkeypatch.setattr("ops.agent_launch._launch_agent_process", boom)

        with pytest.raises(RuntimeError, match="launch \u6545\u610f\u6302"):
            _spawn_agent()

        with db_conn.cursor() as cur:
            cur.execute("SELECT id, status FROM agents_meta")
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "idling"

    def test_persists_config_overlay(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_spawn_agent(config={...}) persists to agents_meta.config_overlay AND
        passes config_overlay= to _launch_agent_process. Both sides must work for
        the per-agent model override to actually take effect at boot.
        """
        from shared.config import settings

        captured: dict[str, object] = {}

        def _record(aid: int, config_overlay: dict[str, object] | None = None, **_kw) -> None:
            captured.update(aid=aid, cfg=config_overlay)

        monkeypatch.setattr("ops.agent_launch._launch_agent_process", _record)  # pyright: ignore[reportUnknownArgumentType]

        new_id = _spawn_agent(spawner="user", config={"llm_model": "gpt-5.6-sol"})

        # column persisted
        with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (new_id,))
            row = cur.fetchone()
        assert row is not None
        assert row[0] == {"llm_model": "gpt-5.6-sol"}

        # launch received the overlay
        assert captured.get("cfg") == {"llm_model": "gpt-5.6-sol"}

    def test_snapshot_reports_effective_model_vision_support(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings.lm, "llm_model", "claude-sonnet-5")
        default_model_agent = _spawn_agent()
        text_only_agent = _spawn_agent(config={"llm_model": "deepseek-v4-pro"})

        default_snapshot = select_one(db_conn, default_model_agent)
        text_only_snapshot = select_one(db_conn, text_only_agent)

        assert default_snapshot is not None
        assert default_snapshot.supports_vision is True
        assert text_only_snapshot is not None
        assert text_only_snapshot.supports_vision is False


class TestResurrectAgent:
    def test_repeat_force_fence_preserves_page_reopen_epoch(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A repeated force creates a newer intent fence without changing the
        real status-transition epoch used to reopen pages on manual resurrect."""
        agent_id = _spawn_agent()
        monkeypatch.setattr("ops.ops_exit.publish_inbound_wake", _noop)
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_pages (agent_id, name, port) VALUES (%s, 'work', 8765)",
                (agent_id,),
            )
        db_conn.commit()

        with _test_pool() as pool:
            _force_mark_terminated(agent_id, pool)
            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT status_changed_at, last_force_terminate_inbound_id "
                    "FROM agents_meta WHERE id = %s",
                    (agent_id,),
                )
                first_agent_row = cur.fetchone()
                cur.execute(
                    "SELECT closed_at FROM agent_pages WHERE agent_id = %s AND name = 'work'",
                    (agent_id,),
                )
                first_page_row = cur.fetchone()
            assert first_agent_row is not None and first_page_row is not None
            assert first_page_row[0] == first_agent_row[0]

            _force_mark_terminated(agent_id, pool)

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT status_changed_at, last_force_terminate_inbound_id "
                "FROM agents_meta WHERE id = %s",
                (agent_id,),
            )
            repeated_agent_row = cur.fetchone()
            cur.execute(
                "SELECT closed_at FROM agent_pages WHERE agent_id = %s AND name = 'work'",
                (agent_id,),
            )
            repeated_page_row = cur.fetchone()
        assert repeated_agent_row is not None and repeated_page_row is not None
        assert repeated_agent_row[0] == first_agent_row[0]
        assert repeated_agent_row[1] > first_agent_row[1]
        assert repeated_page_row[0] == first_page_row[0]

        resurrect_agent(agent_id, resurrected_by="user")

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT closed_at FROM agent_pages WHERE agent_id = %s AND name = 'work'",
                (agent_id,),
            )
            reopened_page_row = cur.fetchone()
        assert reopened_page_row == (None,)

    def test_guarded_resurrect_rejects_chat_below_latest_force_fence(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even without a real status transition, a repeated explicit force
        fences every chat inbound that existed before that latest intent."""
        agent_id = _spawn_agent()
        launched: list[int] = []
        monkeypatch.setattr(
            agent_launch,
            "_launch_agent_process",
            _record_agent_ids(launched),
        )
        monkeypatch.setattr("ops.ops_exit.publish_inbound_wake", _noop)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        trigger_id = shared.db.insert_inbound_message(
            db_conn, agent_id, "work before repeated force", source="user"
        )
        with _test_pool() as pool:
            _force_mark_terminated(agent_id, pool)

        with pytest.raises(ResurrectTriggerStaleError, match="trigger work no longer qualifies"):
            resurrect_agent(
                agent_id,
                resurrected_by="system",
                trigger_inbound_id=trigger_id,
                trigger_inbound_kind="chat",
            )

        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "terminated"
        assert launched == []

    def test_guarded_resurrect_rejects_chat_from_prior_termination(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A watchdog task selected for one death must not revive a later
        explicit kill while its RPC was in flight."""
        agent_id = _spawn_agent()
        launched: list[int] = []
        monkeypatch.setattr(
            agent_launch,
            "_launch_agent_process",
            _record_agent_ids(launched),
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        trigger_id = shared.db.insert_inbound_message(
            db_conn, agent_id, "wake after first death", source="user"
        )

        # The agent came back by another path and was explicitly killed again
        # before the watchdog's original RPC reached its home runner.
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'idling' WHERE id = %s", (agent_id,))
        db_conn.commit()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
            cur.execute(
                "UPDATE inbound_messages "
                "SET created_at = (SELECT status_changed_at FROM agents_meta WHERE id = %s) "
                "                 - interval '1 second' "
                "WHERE id = %s",
                (agent_id, trigger_id),
            )
        db_conn.commit()

        with pytest.raises(ResurrectTriggerStaleError, match="trigger work no longer qualifies"):
            resurrect_agent(
                agent_id,
                resurrected_by="system",
                trigger_inbound_id=trigger_id,
                trigger_inbound_kind="chat",
            )

        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "terminated"
        assert _inbound_rows(db_conn, agent_id) == [("wake after first death", "chat", "user")]
        assert launched == []

    def test_guarded_resurrect_rejects_chat_that_is_no_longer_pending(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A trigger claimed while the RPC is in flight no longer justifies
        launching the terminated owner."""
        agent_id = _spawn_agent()
        launched: list[int] = []
        monkeypatch.setattr(
            agent_launch,
            "_launch_agent_process",
            _record_agent_ids(launched),
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        trigger_id = shared.db.insert_inbound_message(
            db_conn, agent_id, "already handled", source="user"
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE inbound_messages SET status = 'claimed', claimed_at = now() WHERE id = %s",
                (trigger_id,),
            )
        db_conn.commit()

        with pytest.raises(ResurrectTriggerStaleError, match="trigger work no longer qualifies"):
            resurrect_agent(
                agent_id,
                resurrected_by="system",
                trigger_inbound_id=trigger_id,
                trigger_inbound_kind="chat",
            )

        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "terminated"
        assert _inbound_rows(db_conn, agent_id) == [("already handled", "chat", "user")]
        assert launched == []

    def test_guarded_resurrect_accepts_pending_chat_after_current_termination(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pending chat created after the current death still auto-wakes the
        agent, preserving the post-termination delivery contract."""
        agent_id = _spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        trigger_id = shared.db.insert_inbound_message(
            db_conn, agent_id, "new work after death", source="user"
        )
        returned = resurrect_agent(
            agent_id,
            resurrected_by="system",
            trigger_inbound_id=trigger_id,
            trigger_inbound_kind="chat",
        )

        assert returned == agent_id
        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "idling"
        assert _inbound_rows(db_conn, agent_id) == [
            ("new work after death", "chat", "user"),
            ("", "resurrect", "system"),
        ]

    def test_guarded_resurrect_accepts_exact_pending_compact_after_termination(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """UI compact is guarded work too: its exact durable id and expected
        kind qualify only while pending after the current death."""
        agent_id = _spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        compact_id = shared.db.insert_inbound_message(
            db_conn,
            agent_id,
            "",
            source="user",
            kind="compact_request",
        )
        returned = resurrect_agent(
            agent_id,
            resurrected_by="system",
            trigger_inbound_id=compact_id,
            trigger_inbound_kind="compact_request",
        )

        assert returned == agent_id
        assert _agents_row(db_conn, agent_id)[2] == "idling"  # type: ignore[index]
        assert _inbound_rows(db_conn, agent_id) == [
            ("", "compact_request", "user"),
            ("", "resurrect", "system"),
        ]

    def test_guarded_compact_rejects_kind_mismatch_and_claimed_trigger(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The caller's expected kind is part of the CAS, and a compact that
        has already been claimed no longer licenses a new process."""
        agent_id = _spawn_agent()
        launched: list[int] = []
        monkeypatch.setattr(
            agent_launch,
            "_launch_agent_process",
            _record_agent_ids(launched),
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        compact_id = shared.db.insert_inbound_message(
            db_conn,
            agent_id,
            "",
            source="user",
            kind="compact_request",
        )

        with pytest.raises(ResurrectTriggerStaleError):
            resurrect_agent(
                agent_id,
                resurrected_by="system",
                trigger_inbound_id=compact_id,
                trigger_inbound_kind="chat",
            )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE inbound_messages SET status = 'claimed', claimed_at = now() WHERE id = %s",
                (compact_id,),
            )
        db_conn.commit()
        with pytest.raises(ResurrectTriggerStaleError):
            resurrect_agent(
                agent_id,
                resurrected_by="system",
                trigger_inbound_id=compact_id,
                trigger_inbound_kind="compact_request",
            )

        assert _agents_row(db_conn, agent_id)[2] == "terminated"  # type: ignore[index]
        assert launched == []

    def test_guarded_compact_below_force_fence_is_stale(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A force after compact enqueue fences that older work exactly like
        chat, even though no second status transition occurs."""
        agent_id = _spawn_agent()
        launched: list[int] = []
        monkeypatch.setattr(
            agent_launch,
            "_launch_agent_process",
            _record_agent_ids(launched),
        )
        monkeypatch.setattr("ops.ops_exit.publish_inbound_wake", _noop)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        compact_id = shared.db.insert_inbound_message(
            db_conn,
            agent_id,
            "",
            source="user",
            kind="compact_request",
        )
        with _test_pool() as pool:
            _force_mark_terminated(agent_id, pool)

        with pytest.raises(ResurrectTriggerStaleError):
            resurrect_agent(
                agent_id,
                resurrected_by="system",
                trigger_inbound_id=compact_id,
                trigger_inbound_kind="compact_request",
            )
        assert _agents_row(db_conn, agent_id)[2] == "terminated"  # type: ignore[index]
        assert launched == []

    def test_concurrent_guarded_resurrect_launches_once(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even if duplicate watchdog processes reach the home runner, the
        final status/chat CAS permits one marker and one process launch."""
        import threading
        from concurrent.futures import ThreadPoolExecutor

        agent_id = _spawn_agent()
        launched: list[int] = []
        monkeypatch.setattr(
            agent_launch,
            "_launch_agent_process",
            _record_agent_ids(launched),
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        trigger_id = shared.db.insert_inbound_message(db_conn, agent_id, "one wake", source="user")
        update_barrier = threading.Barrier(2)
        barrier_hits: list[str] = []
        original_execute = cast(Callable[..., Any], psycopg.Cursor.execute)

        def _synchronize_guarded_updates(cursor: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
            if (
                threading.current_thread().name.startswith("resurrect-cas")
                and "FROM agents_meta" in str(query)
                and "FOR UPDATE" in str(query)
                and threading.current_thread().name not in barrier_hits
            ):
                barrier_hits.append(threading.current_thread().name)
                update_barrier.wait(timeout=5)
            return original_execute(cursor, query, *args, **kwargs)

        monkeypatch.setattr(psycopg.Cursor, "execute", _synchronize_guarded_updates)

        def _attempt() -> str:
            try:
                resurrect_agent(
                    agent_id,
                    resurrected_by="system",
                    trigger_inbound_id=trigger_id,
                    trigger_inbound_kind="chat",
                )
            except (ResurrectAlreadyAlive, ResurrectTriggerStaleError) as exc:
                return type(exc).__name__
            return "spawned"

        def _attempt_ignoring_index(_index: int) -> str:
            return _attempt()

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="resurrect-cas") as executor:
            results = list(executor.map(_attempt_ignoring_index, range(2)))

        assert results.count("spawned") == 1
        assert next(result for result in results if result != "spawned") in {
            "ResurrectTriggerStaleError",
            "ResurrectAlreadyAlive",
        }
        assert len(barrier_hits) == 2
        assert launched == [agent_id]
        assert _inbound_rows(db_conn, agent_id) == [
            ("one wake", "chat", "user"),
            ("", "resurrect", "system"),
        ]
        assert db_conn.execute(
            "SELECT payload->'resurrection_launch'->'attempts' FROM inbound_messages "
            "WHERE agent_id=%s AND kind='resurrect'",
            (agent_id,),
        ).fetchall() == [(1,)]

    def test_force_row_lock_first_makes_guarded_resurrect_stale(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A force intent holding the agent row lock commits before a waiting
        guarded CAS, which then inserts no marker and creates no session."""
        import threading

        agent_id = _spawn_agent()
        trigger_id = _terminated_with_chat(db_conn, agent_id, "after death")
        force_locked = threading.Event()
        release_force = threading.Event()
        guard_attempted = threading.Event()
        launched: list[int] = []
        failures: list[BaseException] = []
        monkeypatch.setattr(agent_launch, "_launch_agent_process", _record_agent_ids(launched))
        monkeypatch.setattr("ops.ops_exit.publish_inbound_wake", _noop)
        original_execute = cast(Callable[..., Any], psycopg.Cursor.execute)

        def _ordered_execute(cursor: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
            sql = str(query)
            name = threading.current_thread().name
            if name == "force-first" and "SELECT status, pid FROM agents_meta" in sql:
                result = original_execute(cursor, query, *args, **kwargs)
                force_locked.set()
                assert release_force.wait(timeout=5)
                return result
            if name == "guard-waits" and "FROM agents_meta" in sql and "FOR UPDATE" in sql:
                guard_attempted.set()
            return original_execute(cursor, query, *args, **kwargs)

        monkeypatch.setattr(psycopg.Cursor, "execute", _ordered_execute)

        def _force() -> None:
            try:
                with _test_pool() as pool:
                    _force_mark_terminated(agent_id, pool)
            except BaseException as exc:
                failures.append(exc)

        def _guard() -> None:
            try:
                resurrect_agent(
                    agent_id,
                    resurrected_by="system",
                    trigger_inbound_id=trigger_id,
                    trigger_inbound_kind="chat",
                )
            except BaseException as exc:
                failures.append(exc)

        force_thread = threading.Thread(target=_force, name="force-first")
        guard_thread = threading.Thread(target=_guard, name="guard-waits")
        force_thread.start()
        assert force_locked.wait(timeout=5)
        guard_thread.start()
        assert guard_attempted.wait(timeout=5)
        assert launched == []
        release_force.set()
        force_thread.join(timeout=5)
        guard_thread.join(timeout=5)

        assert not force_thread.is_alive() and not guard_thread.is_alive()
        assert len(failures) == 1 and isinstance(failures[0], ResurrectTriggerStaleError)
        assert launched == []
        assert _termination_row(db_conn, agent_id)[:2] == ("terminated", "user")
        assert _resurrect_inbound_count(db_conn, agent_id) == 0

    def test_guarded_session_is_created_under_lock_then_force_kills_exact_session(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once the guarded CAS wins, its exact session exists before the row
        unlocks; a later force waits, then kills that session and wins last."""
        import threading

        from ops.ops_lifecycle import _force_terminate_transaction
        from shared.cluster import session_name

        agent_id = _spawn_agent()
        trigger_id = _terminated_with_chat(db_conn, agent_id, "wake")
        session_created = threading.Event()
        release_guard = threading.Event()
        force_lock_attempted = threading.Event()
        launched: list[int] = []
        killed_sessions: list[str] = []
        failures: list[BaseException] = []

        def _launch(aid: int, **_kwargs: object) -> None:
            launched.append(aid)
            session_created.set()
            assert release_guard.wait(timeout=5)

        class _Supervisor:
            @staticmethod
            def kill_session(name: str, *, graceful: bool) -> tuple[bool, str]:
                assert graceful is False
                killed_sessions.append(name)
                return True, "killed"

        monkeypatch.setattr(agent_launch, "_launch_agent_process", _launch)
        monkeypatch.setattr("ops.ops_exit.native_proc", _Supervisor)
        original_execute = cast(Callable[..., Any], psycopg.Cursor.execute)

        def _observe_force_lock(cursor: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
            if (
                threading.current_thread().name == "force-after-session"
                and "SELECT status, pid FROM agents_meta" in str(query)
            ):
                force_lock_attempted.set()
            return original_execute(cursor, query, *args, **kwargs)

        monkeypatch.setattr(psycopg.Cursor, "execute", _observe_force_lock)

        def _guard() -> None:
            try:
                resurrect_agent(
                    agent_id,
                    resurrected_by="system",
                    trigger_inbound_id=trigger_id,
                    trigger_inbound_kind="chat",
                )
            except BaseException as exc:
                failures.append(exc)

        def _force() -> None:
            try:
                with _test_pool() as pool:
                    _force_terminate_transaction(agent_id, pool, source="user", kill_process=True)
            except BaseException as exc:
                failures.append(exc)

        guard_thread = threading.Thread(target=_guard, name="guard-holds-row")
        force_thread = threading.Thread(target=_force, name="force-after-session")
        guard_thread.start()
        assert session_created.wait(timeout=5)
        force_thread.start()
        assert force_lock_attempted.wait(timeout=5)
        assert killed_sessions == []
        release_guard.set()
        guard_thread.join(timeout=5)
        force_thread.join(timeout=5)

        assert not failures
        assert launched == [agent_id]
        assert killed_sessions == [session_name(f"agent-{agent_id}")]
        assert _termination_row(db_conn, agent_id) == ("terminated", "user", True)

    def test_child_starting_sees_committed_resurrect_authorization(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Popen runs after authorization commits, without holding metadata locks."""
        from agent import _starting

        agent_id = _spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (agent_id,))
        db_conn.commit()
        trigger_id = shared.db.insert_inbound_message(db_conn, agent_id, "wake", source="user")
        admitted: list[int] = []

        def _launch(_aid: int, **kwargs: object) -> None:
            attempt = kwargs["resurrect_attempt"]
            assert isinstance(attempt, tuple)
            command: object = cast(tuple[object, ...], attempt)[0]
            assert type(command) is int
            # This independent connection must acquire the lock immediately;
            # the real launcher must never run inside its authorization TX.
            db_conn.rollback()
            assert db_conn.execute(
                "SELECT status,pid FROM agents_meta WHERE id=%s FOR UPDATE NOWAIT", (agent_id,)
            ).fetchone() == ("idling", None)
            assert db_conn.execute(
                "SELECT status,payload->'resurrection_launch'->>'attempts' "
                "FROM inbound_messages WHERE id=%s AND agent_id=%s",
                (command, agent_id),
            ).fetchone() == ("pending", "1")
            db_conn.commit()
            with pytest.raises(ResurrectError, match="launch identity"):
                _starting.claim_agent_row(agent_id)
            _starting.claim_agent_row(agent_id, resurrect_command_id=command)
            with pytest.raises(ResurrectError, match="allocation changed"):
                _starting.claim_agent_row(agent_id, resurrect_command_id=command)
            admitted.append(command)

        monkeypatch.setattr(agent_launch, "_launch_agent_process", _launch)

        resurrect_agent(
            agent_id,
            resurrected_by="system",
            trigger_inbound_id=trigger_id,
            trigger_inbound_kind="chat",
        )
        assert len(admitted) == 1
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id=%s", (agent_id,))
            assert cur.fetchone() == ("running",)

    def test_pause_waits_for_initial_resurrect_then_final_sweep_wins(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resurrection holding the machine share lock may commit its exact
        session, but pause waits for it and the final sweep kills it last."""
        import threading

        from shared.cluster import session_name

        agent_id = _spawn_agent()
        trigger_id = _terminated_on_machine_with_chat(
            db_conn, agent_id, "pause-race-initial", "wake"
        )
        session_created = threading.Event()
        release_resurrect = threading.Event()
        pause_latch_attempted = threading.Event()
        killed_sessions: list[str] = []
        failures: list[BaseException] = []

        def _launch(_aid: int, **_kwargs: object) -> None:
            session_created.set()
            assert release_resurrect.wait(timeout=5)

        class _Supervisor:
            @staticmethod
            def kill_session(name: str, *, graceful: bool) -> tuple[bool, str]:
                assert graceful is False
                killed_sessions.append(name)
                return True, "killed"

        monkeypatch.setattr(agent_launch, "_launch_agent_process", _launch)
        monkeypatch.setattr("ops.ops_exit.native_proc", _Supervisor)
        original_execute = cast(Callable[..., Any], psycopg.Cursor.execute)

        def _observe_latch(cursor: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
            if (
                threading.current_thread().name == "pause-after-resurrect"
                and "UPDATE machines SET paused_at" in str(query)
            ):
                pause_latch_attempted.set()
            return original_execute(cursor, query, *args, **kwargs)

        monkeypatch.setattr(psycopg.Cursor, "execute", _observe_latch)

        def _resurrect() -> None:
            try:
                resurrect_agent(
                    agent_id,
                    resurrected_by="system",
                    trigger_inbound_id=trigger_id,
                    trigger_inbound_kind="chat",
                )
            except BaseException as exc:
                failures.append(exc)

        def _pause() -> None:
            try:
                _pause_and_force_sweep("pause-race-initial")
            except BaseException as exc:
                failures.append(exc)

        resurrect_thread = threading.Thread(target=_resurrect, name="resurrect-before-pause")
        pause_thread = threading.Thread(target=_pause, name="pause-after-resurrect")
        resurrect_thread.start()
        assert session_created.wait(timeout=5)
        pause_thread.start()
        assert pause_latch_attempted.wait(timeout=5)
        assert killed_sessions == []
        release_resurrect.set()
        resurrect_thread.join(timeout=5)
        pause_thread.join(timeout=5)

        assert not failures
        assert killed_sessions == [session_name(f"agent-{agent_id}")]
        assert _termination_row(db_conn, agent_id) == ("terminated", "user", True)

    def test_pause_before_resurrect_retry_prevents_second_session(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A retry re-locks the machine admission row. Pause may win after the
        first committed session, sweep it, and prevent any second launch."""
        import threading

        from shared.cluster import session_name

        agent_id = _spawn_agent()
        trigger_id = _terminated_on_machine_with_chat(db_conn, agent_id, "pause-race-retry", "wake")
        confirm_failed = threading.Event()
        retry_lock_attempted = threading.Event()
        release_retry = threading.Event()
        launches: list[int] = []
        killed_sessions: list[str] = []
        failures: list[BaseException] = []
        retry_barrier_hits: list[str] = []

        def _launch(aid: int, **_kwargs: object) -> str:
            launches.append(aid)
            return f"test-resurrect-attempt-{aid}"

        class _Supervisor:
            @staticmethod
            def kill_session(name: str, *, graceful: bool) -> tuple[bool, str]:
                assert graceful is False
                killed_sessions.append(name)
                return True, "killed"

        def _never_confirms(_aid: int, _attempt: str | None = None) -> None:
            confirm_failed.set()
            raise RuntimeError("child did not claim")

        monkeypatch.setattr(agent_launch, "_launch_agent_process", _launch)
        monkeypatch.setattr(agent_launch, "_wait_for_agent_claim", _never_confirms)
        monkeypatch.setattr(agent_launch, "_LAUNCH_RETRY_BASE_BACKOFF_SEC", 0.0)
        monkeypatch.setattr("ops.ops_exit.native_proc", _Supervisor)
        original_execute = cast(Callable[..., Any], psycopg.Cursor.execute)

        def _block_retry_machine_lock(cursor: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
            if (
                threading.current_thread().name == "resurrect-retry"
                and "SELECT paused_at FROM machines" in str(query)
                and confirm_failed.is_set()
            ):
                retry_barrier_hits.append("retry-machine-lock")
                retry_lock_attempted.set()
                assert release_retry.wait(timeout=5)
            return original_execute(cursor, query, *args, **kwargs)

        monkeypatch.setattr(psycopg.Cursor, "execute", _block_retry_machine_lock)

        def _resurrect() -> None:
            try:
                resurrect_agent(
                    agent_id,
                    resurrected_by="system",
                    trigger_inbound_id=trigger_id,
                    trigger_inbound_kind="chat",
                )
            except BaseException as exc:
                failures.append(exc)

        retry_thread = threading.Thread(target=_resurrect, name="resurrect-retry")
        retry_thread.start()
        assert retry_lock_attempted.wait(timeout=5)
        _pause_and_force_sweep("pause-race-retry")
        release_retry.set()
        retry_thread.join(timeout=5)

        assert len(failures) == 1 and isinstance(failures[0], MachinePaused)
        assert retry_barrier_hits == ["retry-machine-lock"]
        assert launches == [agent_id]
        assert db_conn.execute(
            "SELECT payload->'resurrection_launch_attempts' FROM inbound_messages WHERE id=%s",
            (trigger_id,),
        ).fetchone() == (1,)
        assert db_conn.execute(
            "SELECT payload->'resurrection_launch'->'attempts' FROM inbound_messages "
            "WHERE agent_id=%s AND kind='resurrect'",
            (agent_id,),
        ).fetchall() == [(1,)]
        assert killed_sessions == [session_name(f"agent-{agent_id}")]
        assert _termination_row(db_conn, agent_id) == ("terminated", "user", True)

    @pytest.mark.parametrize("reentry_kind", ["manual", "chat", "compact", "crash"])
    def test_paused_home_rejects_every_resurrection_admission(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        reentry_kind: str,
    ) -> None:
        """The machine latch is a shared admission fence for explicit,
        pending-work, and controller resurrection paths."""
        agent_id = _spawn_agent()
        launched: list[int] = []
        monkeypatch.setattr(agent_launch, "_launch_agent_process", _record_agent_ids(launched))
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO machines (name, role, paused_at) "
                "VALUES ('paused-runner', ARRAY['agent-runner'], now())"
            )
            cur.execute(
                "UPDATE agents_meta SET machine='paused-runner', status='terminated', "
                "termination_source='reaper', last_resurrect_at=now() WHERE id=%s "
                "RETURNING status_changed_at, last_resurrect_at",
                (agent_id,),
            )
            claim_row = cur.fetchone()
            assert claim_row is not None
            termination_epoch, claimed_at = claim_row
        db_conn.commit()
        kwargs: dict[str, object] = {}
        if reentry_kind in {"chat", "compact"}:
            inbound_kind = "chat" if reentry_kind == "chat" else "compact_request"
            trigger_id = shared.db.insert_inbound_message(
                db_conn, agent_id, "work", source="user", kind=inbound_kind
            )
            kwargs = {
                "trigger_inbound_id": trigger_id,
                "trigger_inbound_kind": inbound_kind,
            }
        elif reentry_kind == "crash":
            kwargs = {
                "auto_claim": AutoResurrectClaim(
                    agent_id=agent_id,
                    termination_source=TerminationSource.REAPER,
                    termination_epoch=termination_epoch,
                    claim_kind="crash",
                    claimed_at=claimed_at,
                )
            }

        with pytest.raises(MachinePaused):
            resurrect_agent(agent_id, resurrected_by="system", **kwargs)  # pyright: ignore[reportArgumentType]

        assert launched == []
        assert _termination_row(db_conn, agent_id)[:2] == ("terminated", "reaper")
        assert _resurrect_inbound_count(db_conn, agent_id) == 0

    def test_wedged_claim_resurrects_exact_claimed_death(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wedged final CAS accepts the exact source, death epoch, and
        controller claim stamp produced by the transition."""
        agent_id = _spawn_agent()
        launched: list[int] = []
        monkeypatch.setattr(agent_launch, "_launch_agent_process", _record_agent_ids(launched))
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status='terminated', termination_source='reaper', "
                "last_wedged_check_at=now() WHERE id=%s "
                "RETURNING status_changed_at, last_wedged_check_at",
                (agent_id,),
            )
            claim_row = cur.fetchone()
            assert claim_row is not None
            death_epoch, claimed_at = claim_row
        db_conn.commit()

        resurrect_agent(
            agent_id,
            resurrected_by="system",
            auto_claim=AutoResurrectClaim(
                agent_id=agent_id,
                termination_source=TerminationSource.REAPER,
                termination_epoch=death_epoch,
                claim_kind="wedged",
                claimed_at=claimed_at,
            ),
        )

        assert _agents_row(db_conn, agent_id)[2] == "idling"  # type: ignore[index]
        assert _resurrect_inbound_count(db_conn, agent_id) == 1
        assert launched == [agent_id]

    def test_wedged_claim_cannot_reverse_later_force(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A user force after wedged claimed the death makes the old final CAS
        stale without writing a marker or creating a session."""
        agent_id = _spawn_agent()
        launched: list[int] = []
        monkeypatch.setattr(agent_launch, "_launch_agent_process", _record_agent_ids(launched))
        monkeypatch.setattr("ops.ops_exit.publish_inbound_wake", _noop)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status='terminated', termination_source='reaper', "
                "last_wedged_check_at=now() WHERE id=%s "
                "RETURNING status_changed_at, last_wedged_check_at",
                (agent_id,),
            )
            claim_row = cur.fetchone()
            assert claim_row is not None
            death_epoch, claimed_at = claim_row
        db_conn.commit()
        claim = AutoResurrectClaim(
            agent_id=agent_id,
            termination_source=TerminationSource.REAPER,
            termination_epoch=death_epoch,
            claim_kind="wedged",
            claimed_at=claimed_at,
        )
        with _test_pool() as pool:
            _force_mark_terminated(agent_id, pool)

        with pytest.raises(ResurrectClaimStaleError):
            resurrect_agent(agent_id, resurrected_by="system", auto_claim=claim)

        assert _termination_row(db_conn, agent_id)[:2] == ("terminated", "user")
        assert _resurrect_inbound_count(db_conn, agent_id) == 0
        assert launched == []

    def test_wedged_claim_rejects_same_source_death_aba(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact death epoch prevents an old wedged claim from matching a
        later death that happens to reuse its source and claim timestamp."""
        agent_id = _spawn_agent()
        launched: list[int] = []
        monkeypatch.setattr(agent_launch, "_launch_agent_process", _record_agent_ids(launched))
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status='terminated', termination_source='reaper', "
                "last_wedged_check_at=now() WHERE id=%s "
                "RETURNING status_changed_at, last_wedged_check_at",
                (agent_id,),
            )
            claim_row = cur.fetchone()
            assert claim_row is not None
            old_epoch, claimed_at = claim_row
        db_conn.commit()
        claim = AutoResurrectClaim(
            agent_id=agent_id,
            termination_source=TerminationSource.REAPER,
            termination_epoch=old_epoch,
            claim_kind="wedged",
            claimed_at=claimed_at,
        )
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status='idling' WHERE id=%s", (agent_id,))
            cur.execute(
                "UPDATE agents_meta SET status='terminated', termination_source='reaper', "
                "last_wedged_check_at=%s WHERE id=%s",
                (claimed_at, agent_id),
            )
        db_conn.commit()

        with pytest.raises(ResurrectClaimStaleError):
            resurrect_agent(agent_id, resurrected_by="system", auto_claim=claim)

        assert _termination_row(db_conn, agent_id)[:2] == ("terminated", "reaper")
        assert _resurrect_inbound_count(db_conn, agent_id) == 0
        assert launched == []

    def test_resurrects_terminated_agent_and_inserts_resurrect_inbound(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """terminated → unclaimed idling + start process + auto INSERT a kind='resurrect'
        source=resurrected_by inbound; when the new process starts, claim dispatches it as a lifecycle
        marker appended to messages."""
        agent_id = _spawn_agent()
        # simulate terminate path: UPDATE 'idling' → 'terminated' (semantics from loop.py)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()

        returned = resurrect_agent(agent_id, resurrected_by="user", prompt="resume work")

        assert returned == agent_id
        row = _agents_row(db_conn, agent_id)
        assert row is not None
        assert (
            row[2] == "idling"
        )  # unclaimed, waiting for the process to claim and UPDATE 'running'
        # resurrect inserts lifecycle inbound — content empty, trigger written to source field;
        # prompt as chat inbound follows in the same transaction
        rows = _inbound_rows(db_conn, agent_id)
        assert rows == [("", "resurrect", "user"), ("resume work", "chat", "user")]

    def test_resurrect_without_prompt_inserts_only_lifecycle_inbound(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The UI resurrect button is a pure lifecycle event — no prompt. Only
        the kind='resurrect' marker inbound is written; no chat inbound. The
        agent still wakes (the marker is the "ok I'm awake" signal)."""
        agent_id = _spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()

        returned = resurrect_agent(agent_id, resurrected_by="user")

        assert returned == agent_id
        assert _inbound_rows(db_conn, agent_id) == [("", "resurrect", "user")]

    def test_resurrect_records_resurrected_by_in_inbound_source(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resurrected_by is written as-is into the inbound source field (not into content), so that claim
        can compose it into the lifecycle marker during dispatch. SDK paths pass 'agent:N', gateway passes 'user'."""
        agent_id = _spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()

        resurrect_agent(agent_id, resurrected_by="agent:42", prompt="resume work")

        rows = _inbound_rows(db_conn, agent_id)
        assert rows == [
            ("", "resurrect", "agent:42"),
            ("resume work", "chat", "agent:42"),
        ]

    def test_resurrect_with_prompt_chat_inbound_has_wrappable_source(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A resurrect with a prompt writes two inbounds (lifecycle + chat); the chat inbound reuses
        resurrected_by as its source — that value must survive envelope wrap, otherwise
        the new process dies with a ValueError on its first claim (agent-240 incident)."""
        agent_id = _spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()

        resurrect_agent(agent_id, resurrected_by="user", prompt="catch up with #341")

        rows = _inbound_rows(db_conn, agent_id)
        assert rows == [
            ("", "resurrect", "user"),
            ("catch up with #341", "chat", "user"),
        ]
        # The chat inbound's source must be a valid value accepted by the claim-side wrap
        for content, kind, source in rows:
            if kind == "chat":
                assert source is not None
                wrap_inbound(
                    content,
                    source,
                )  # raises ValueError on illegal source

    def test_resurrect_nonexistent_raises_agent_not_found(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with pytest.raises(AgentNotFound, match="does not exist"):
            resurrect_agent(9999, resurrected_by="user", prompt="test")

    @pytest.mark.parametrize(
        "alive_status",
        ["running", "idling", "restarting"],
    )
    def test_resurrect_alive_agent_raises_already_alive(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        alive_status: str,
    ) -> None:
        """Any status other than 'terminated' cannot be resurrected — only 'terminated' is a valid source state.

        Full parametrization locks the contract that "resurrect refuses all states that are still alive or not fully dead".
        Historically only running/idling/restarting were tested. The complete current
        non-terminal set is covered explicitly — a regression that changed the guard
        to `if current in [...]` and missed a value would silently let resurrect
        send a revival notification to an agent that is "still running / still init'ing",
        with the production consequence of a dual-process race on the same agent_id.
        """
        agent_id = _spawn_agent()
        # the helper leaves 'idling'; other statuses are explicitly set via UPDATE for the test
        if alive_status != "idling":
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET status = %s WHERE id = %s",
                    (alive_status, agent_id),
                )
            db_conn.commit()
        with pytest.raises(ResurrectAlreadyAlive, match=alive_status):
            resurrect_agent(agent_id, resurrected_by="user", prompt="test")
        # The failure path must not insert inbound (transaction inner raise prevents commit)
        assert _inbound_count(db_conn, agent_id) == 0

    def test_resurrect_select_update_race_does_not_insert_inbound(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SELECT sees 'terminated' → after SELECT the row is concurrently changed to 'idling' → UPDATE
        WHERE status='terminated' hits 0 rows — at this point we must **not** proceed to INSERT a fake revival
        notification for a already-live agent (that would send an hallucination signal to the running process).

        Simulate: make the first fetchone falsely report 'terminated' while the underlying row is actually 'idling'
        — equivalent to "status was rewritten after SELECT". The code must raise when UPDATE rowcount=0.
        """
        agent_id = _spawn_agent()  # real status='idling'

        original_execute = cast(Callable[..., Any], psycopg.Cursor.execute)
        original_fetchone = psycopg.Cursor.fetchone
        status_select_cursors: set[int] = set()

        def tracking_execute(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
            result = original_execute(self, query, *args, **kwargs)
            if query == "SELECT status,machine FROM agents_meta WHERE id = %s FOR UPDATE":
                status_select_cursors.add(id(self))
            return result

        def lying_fetchone(self: Any) -> Any:
            if id(self) in status_select_cursors:
                status_select_cursors.remove(id(self))
                return ("terminated", machine_name())  # enter UPDATE while preserving placement
            return original_fetchone(self)

        monkeypatch.setattr(psycopg.Cursor, "execute", tracking_execute)
        monkeypatch.setattr(psycopg.Cursor, "fetchone", lying_fetchone)

        with pytest.raises(ResurrectAlreadyAlive, match="concurrently modified"):
            resurrect_agent(agent_id, resurrected_by="user", prompt="test")

        # key invariant 1: did not deliver a fake notification to a live agent
        monkeypatch.undo()  # restore fetchone so subsequent queries work
        assert _inbound_count(db_conn, agent_id) == 0
        # key invariant 2: status unchanged (UPDATE 0 rows + raise rolls back entire transaction)
        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "idling"

    def test_subclasses_inherit_from_resurrect_error(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both subclasses belong to ResurrectError — a coarse catch with ResurrectError can catch them."""
        with pytest.raises(ResurrectError):
            resurrect_agent(9999, resurrected_by="user", prompt="test")

    def test_resurrect_clears_stale_pid_and_started_at(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The UPDATE that changes 'terminated' → 'idling' must **also clear** pid /
        started_at — otherwise the fields left from the previous running round become ghost data;
        operations like `ps -p <stale_pid>` / `kill <stale_pid>` would misjudge (agent 44 incident).

        invariant: pid and started_at are only filled during 'running'; any transition back to 'idling'
        must reset them to NULL, aligning with the new row default from spawn.
        """
        agent_id = _spawn_agent()
        # simulate "fields left from the previous running round" — directly UPDATE to fill pid + started_at,
        # then switch to terminated (terminate finalize mark_agent_exited_op does not touch pid/started_at)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'terminated', pid = 99999, started_at = now() "
                "WHERE id = %s",
                (agent_id,),
            )
        db_conn.commit()
        # pre-check: confirm stale fields are indeed set
        pid, started_at = _agents_pid_started_at(db_conn, agent_id)
        assert pid == 99999 and started_at is not None

        resurrect_agent(agent_id, resurrected_by="user", prompt="test")

        pid, started_at = _agents_pid_started_at(db_conn, agent_id)
        assert pid is None, f"resurrect did not clear stale pid: {pid}"
        assert started_at is None, f"resurrect did not clear stale started_at: {started_at}"

    def test_resurrect_launch_failure_forces_terminated(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """launch fails → UPDATE status='terminated' + re-raise (avoids being stuck
        with a permanent 'idling' residue). The caller can retry resurrect and continue from 'terminated'."""
        # first use noop to let the create+launch helper complete; then switch to boom
        agent_id = _spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()

        def boom(_id: int, **_kw: object) -> None:
            raise RuntimeError("launch \u6545\u610f\u6302")

        # disable retries: this test only verifies the "permanent failure -> force-terminate + re-raise" contract,
        # the retry path is covered separately by TestLaunchRetry (otherwise it would sleep + repeatedly _require_released_agent_session).
        monkeypatch.setattr("ops.agent_launch._LAUNCH_MAX_RETRIES", 0)
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", boom)

        with pytest.raises(RuntimeError, match="launch \u6545\u610f\u6302"):
            resurrect_agent(agent_id, resurrected_by="user", prompt="test")

        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "terminated"


class TestRespawnAgent:
    def _setup_restarting(
        self,
        db: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
        original_source: str,
    ) -> int:
        """spawn → simulate agent receiving 'restart' inbound and transitioning to 'restarting', return agent_id."""
        agent_id = _spawn_agent()
        with db.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'restarting' WHERE id = %s", (agent_id,))
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
                "VALUES (%s, '', 'restart', %s, 'done')",
                (agent_id, original_source),
            )
        db.commit()
        return agent_id

    def test_respawn_inserts_restart_completed_with_original_source(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """respawn 'restarting' → 'idling' + INSERT kind='restart_completed'
        source=source of original 'restart' inbound. When the new process starts, claim writes
        lifecycle marker using this source to compose a trigger identifier like "by user"."""
        agent_id = self._setup_restarting(db_conn, monkeypatch, original_source="user")

        ok = respawn_agent(agent_id)
        assert ok is True

        # status moved back to unclaimed idling, waiting for process to claim and UPDATE 'running'
        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "idling"

        # restart inbound (status='done') + new restart_completed inbound (status='pending')
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT content, kind, source, status FROM inbound_messages "
                "WHERE agent_id = %s ORDER BY id ASC",
                (agent_id,),
            )
            rows = cur.fetchall()
        assert rows == [
            ("", "restart", "user", "done"),
            ("", "restart_completed", "user", "pending"),
        ]

    def test_respawn_clears_stale_pid_and_started_at(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """respawn 'restarting' → 'idling' also clears pid/started_at. Restart also
        transitions from running (when claim receives restart inbound, status='running'), so pid/
        started_at inevitably have stale values; not clearing them would cause the same ghost-field problem as resurrect."""
        agent_id = self._setup_restarting(db_conn, monkeypatch, original_source="user")
        # after _setup_restarting, status='restarting' but pid/started_at are not filled,
        # simulate "traces left from previous running round"
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET pid = 99999, started_at = now() WHERE id = %s",
                (agent_id,),
            )
        db_conn.commit()

        respawn_agent(agent_id)

        pid, started_at = _agents_pid_started_at(db_conn, agent_id)
        assert pid is None and started_at is None

    def test_respawn_returns_false_when_status_not_restarting(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """respawn returns False for non-'restarting' status (race-safe noop) — does not raise,
        does not INSERT inbound, does not launch process. The restarter should continue polling."""
        agent_id = _spawn_agent()  # status='idling'

        ok = respawn_agent(agent_id)
        assert ok is False
        # no inbound inserted, status unchanged
        assert _inbound_count(db_conn, agent_id) == 0
        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "idling"

    def test_respawn_raises_when_no_prior_restart_inbound_and_forces_terminated(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """status='restarting' must have a prior 'restart' inbound; if DB integrity is broken
        (manual UPDATE / migration bug), first mark status 'terminated' commit then raise
        — to avoid the agent being stuck with 'idling' and no process permanently
        (the restarter won't trigger again because status is no longer 'restarting'; caller can resurrect to retry)."""
        agent_id = _spawn_agent()
        # directly UPDATE 'restarting' but **not** insert 'restart' inbound — breaks invariant
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'restarting' WHERE id = %s", (agent_id,))
        db_conn.commit()

        with pytest.raises(RuntimeError, match="DB integrity violated"):
            respawn_agent(agent_id)

        # before raise, status switched to 'terminated' (avoids stuck 'idling' with no process)
        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "terminated"

    def test_respawn_uses_latest_restart_inbound_source(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple 'restart' inbounds (history of multiple restarts) → take the source of the latest one."""
        agent_id = _spawn_agent()
        with db_conn.cursor() as cur:
            # old one (previous restart, already done)
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
                "VALUES (%s, '', 'restart', 'user', 'done')",
                (agent_id,),
            )
            # current restart (already done, triggered the restarting transition)
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
                "VALUES (%s, '', 'restart', 'system', 'done')",
                (agent_id,),
            )
            cur.execute("UPDATE agents_meta SET status = 'restarting' WHERE id = %s", (agent_id,))
        db_conn.commit()

        respawn_agent(agent_id)

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT source FROM inbound_messages WHERE agent_id = %s "
                "AND kind = 'restart_completed'",
                (agent_id,),
            )
            row = cur.fetchone()
        assert row is not None and row[0] == "system"  # take the source of the latest one

    def test_respawn_launch_failure_forces_terminated(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """launch fails → UPDATE status='terminated' + re-raise (avoids permanent 'idling' residue). Caller can resurrect to retry."""
        # _setup_restarting internally monkeypatches lambda → None; then boom after setup
        agent_id = self._setup_restarting(db_conn, monkeypatch, original_source="user")

        def boom(_id: int, **_kw: object) -> None:
            raise RuntimeError("launch \u6545\u610f\u6302")

        # disable retries: same as resurrect version, only verify force-terminate + re-raise contract.
        monkeypatch.setattr("ops.agent_launch._LAUNCH_MAX_RETRIES", 0)
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", boom)

        with pytest.raises(RuntimeError, match="launch \u6545\u610f\u6302"):
            respawn_agent(agent_id)

        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "terminated"

    def test_resurrect_and_respawn_are_symmetric_lifecycle_inbound_writers(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both functions perform 'UPDATE status + INSERT lifecycle inbound + launch' —
        symmetric from the AgentStatus enum perspective: terminated→idling+resurrect inbound,
        restarting→idling+restart_completed inbound.
        This test locks the symmetry in place so that if one side adds new side effects without updating the other symmetrically, the test will fail."""

        # resurrect path
        a = _spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (a,))
        db_conn.commit()
        resurrect_agent(a, resurrected_by="user", prompt="resume")

        # respawn path
        b = self._setup_restarting(db_conn, monkeypatch, original_source="user")
        respawn_agent(b)

        # both agents back to 'idling' and each has lifecycle pending inbound
        # (here with prompt, resurrect also has an extra chat inbound)
        assert _agents_row(db_conn, a)[2] == AgentStatus.IDLING  # type: ignore[index]
        assert _agents_row(db_conn, b)[2] == AgentStatus.IDLING  # type: ignore[index]
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT agent_id, kind FROM inbound_messages "
                "WHERE agent_id IN (%s, %s) AND status = 'pending' ORDER BY agent_id, id",
                (a, b),
            )
            rows = cur.fetchall()
        assert rows == [(a, "resurrect"), (a, "chat"), (b, "restart_completed")]


@pytest.mark.real_agent_launch
class TestLaunchConfirm:
    """`_launch_agent_process` must confirm that the child python process has actually reached
    `claim_agent_row` (i.e., status has left 'idling'), not just "process spawn succeeded".
    A successful spawn = "I launched this process", which does **not** equal "that process actually started running".
    In production, spawn succeeded but the child python immediately crashed (early import/config failure
    / immediate exit) → status stuck 'idling' permanently fossilized.

    The fake native supervisor makes `new_session` return True (process "launched"), and then nothing
    happens (child never starts, status is never changed by anyone).
    Resurrect/respawn paths (confirm=True, inline): confirm timeout raise →
    `_launch_or_force_terminated` catches → 'terminated'.
    Spawn path (confirm=False): spawn does not inline confirm, returns after launch without raising;
    confirmation is instead handled off-path by `_confirm_launch_or_force_terminated` — if child never started,
    force 'terminated'; if already claimed, leave alive.
    """

    @staticmethod
    def _fake_launch_success(monkeypatch: pytest.MonkeyPatch) -> None:
        """Make the native supervisor's `new_session` return True without starting any real process —
        simulating "process spawn succeeded but child python immediately crashed". `kill_session`
        (kill-stale) is also stubbed as noop (resurrect/respawn first call _require_released_agent_session).

        `has_session` answers False, which is what the simulated failure means: the
        launched child is gone, so the confirm gets no deadline extension and fails
        at the first deadline exactly as it did before extensions existed."""

        class _FakeSupervisor:
            @staticmethod
            def new_session(*_a, **_kw):
                return True

            @staticmethod
            def kill_session(*_a, **_kw):
                return (True, "noop")

            @staticmethod
            def has_session(*_a, **_kw):
                return False

        monkeypatch.setattr("ops.agent_launch.native_proc", lambda: _FakeSupervisor)
        # These tests exercise launch confirmation, not credential projection;
        # the fake supervisor never starts a child that could consume this env.
        monkeypatch.setattr("ops.agent_launch.agent_spawn_env_dict", dict)

    @staticmethod
    def _shrink_confirm_timeout(monkeypatch: pytest.MonkeyPatch, timeout_sec: float = 0.1) -> None:
        """Shorten confirm timeout so tests are fast (default 5s is too slow).
        Poll interval is shortened too. Production values are module constants;
        tests override via setattr.

        The child in these tests *never* claims (fake subprocess.run is a no-op),
        so nothing races the deadline — the outcome (timeout → force terminated)
        is identical for any timeout value, only faster. A slow-runner poll stall
        just lands past the deadline and still forces terminated. So 0.1s is safe
        and there is no real child to falsely time out.

        Also disables launch retries (`_LAUNCH_MAX_RETRIES=0`): the confirm-timeout
        tests assert the single-attempt force-terminate outcome; retrying a
        permanently-non-claiming child would only multiply the confirm wait and
        shell out to `_require_released_agent_session`. The retry path is covered by
        TestLaunchRetry."""
        monkeypatch.setattr("ops.agent_launch.LAUNCH_CONFIRM_TIMEOUT_SEC", timeout_sec)
        monkeypatch.setattr("ops.agent_launch._LAUNCH_CONFIRM_POLL_INTERVAL_SEC", 0.02)
        monkeypatch.setattr("ops.agent_launch._LAUNCH_MAX_RETRIES", 0)

    def test_spawn_does_not_block_or_raise_on_confirm(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """spawn launches with confirm=False — the spawn reports OK but the child
        never claims, and spawn still returns its id WITHOUT raising (the
        launch-confirm is off-path now). The row stays 'idling' for the
        off-path confirm / reaper to resolve."""
        self._fake_launch_success(monkeypatch)

        new_id = _spawn_agent()  # no raise even though the child never claims

        with db_conn.cursor() as cur:
            cur.execute("SELECT id, status FROM agents_meta")
            rows = cur.fetchall()
        assert rows == [(new_id, "idling")]

    @pytest.mark.flaky  # real confirm-timeout poll loop (child never claims)
    def test_off_path_confirm_forces_terminated_when_child_never_claims(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The off-path confirm forces a row that never leaves 'idling' to
        'terminated' (a silently-failed launch) so it does not linger until the
        reaper. This is the spawn-path replacement for the inline confirm."""
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        agent_id = _spawn_agent()
        monkeypatch.undo()
        self._shrink_confirm_timeout(monkeypatch)

        _confirm_launch_or_force_terminated(agent_id)

        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "terminated"

    def test_off_path_confirm_leaves_claimed_child_alone(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the child claimed (status left 'idling'), the off-path confirm
        is a no-op — it must not terminate a live agent that merely claimed late."""
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        agent_id = _spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'running', pid = 12345 WHERE id = %s", (agent_id,)
            )
        db_conn.commit()
        # Verify the UPDATE is visible before proceeding — on slow CI runners
        # the first poll inside _wait_for_agent_claim may race
        # with the write becoming visible.
        row_check = _agents_row(db_conn, agent_id)
        assert row_check is not None and row_check[2] == "running", (
            f"pre-condition failed: expected 'running', got {row_check}"
        )
        monkeypatch.undo()
        self._shrink_confirm_timeout(monkeypatch)

        _confirm_launch_or_force_terminated(agent_id)

        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "running"

    @pytest.mark.flaky  # real confirm-timeout poll loop (child never claims)
    def test_resurrect_forces_terminated_when_child_never_claims_row(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resurrect → spawn OK but child never started → confirm timeout → raise →
        `_launch_or_force_terminated` catches it and changes to 'terminated'. The caller can retry
        resurrect from 'terminated'."""
        # setup: first use noop launch for spawn (this class real_agent_launch opt-out of
        # global guard, so setup spawn must stub itself to avoid a real process), then change to 'terminated'
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", lambda _id, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
        agent_id = _spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,))
        db_conn.commit()
        # now restore _launch_agent_process, let it go through the full (subprocess + confirm) path,
        # but subprocess is fake and confirm timeout is shortened
        monkeypatch.undo()
        self._fake_launch_success(monkeypatch)
        self._shrink_confirm_timeout(monkeypatch)

        with pytest.raises(RuntimeError, match=r"pid stayed NULL|did not reach"):
            resurrect_agent(agent_id, resurrected_by="user", prompt="test")

        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "terminated"

    @pytest.mark.flaky  # real confirm-timeout poll loop (child never claims)
    def test_respawn_forces_terminated_when_child_never_claims_row(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """respawn same as resurrect path: spawn OK but child never started → confirm raise
        → force_terminated catches."""
        # setup: spawn → simulate 'restarting' + restart inbound (noop launch: this class
        # real_agent_launch opt-out of global guard, so setup spawn must stub itself to avoid a real process)
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", lambda _id, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
        agent_id = _spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'restarting' WHERE id = %s", (agent_id,))
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
                "VALUES (%s, '', 'restart', 'user', 'done')",
                (agent_id,),
            )
        db_conn.commit()
        monkeypatch.undo()
        self._fake_launch_success(monkeypatch)
        self._shrink_confirm_timeout(monkeypatch)

        with pytest.raises(RuntimeError, match=r"pid stayed NULL|did not reach"):
            respawn_agent(agent_id)

        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "terminated"


class _FakeClock:
    """Virtual monotonic clock for the launch-confirm poll loop.

    `sleep` advances the clock instead of blocking, so the real production
    deadlines (45s, extended to 120s) are exercised in milliseconds AND the
    result does not depend on how loaded the box running the test is — which is
    the exact condition these tests are about. `on_tick` fires after each
    advance, which is how a test makes the child claim its row at a chosen
    virtual instant.
    """

    def __init__(self, on_tick=None) -> None:
        self.now = 0.0
        self._on_tick = on_tick  # pyright: ignore[reportUnknownMemberType]

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self._on_tick is not None:  # pyright: ignore[reportUnknownMemberType]
            self._on_tick(self.now)  # pyright: ignore[reportUnknownMemberType]


class TestLaunchConfirmExtension:
    """A launched process that is still alive at the confirm deadline gets one
    bounded extension instead of having its row taken away mid-boot.

    The 2026-07-30 incident: under box load the child's pre-flip segment (python
    startup + imports + `assert_schema_current` + the placement SELECT) outran
    the 10s window. Nothing in the row can distinguish that from a dead launch —
    'idling' with no pid either way — so the confirm force-terminated a child
    that was seconds from claiming, the child's own CAS then matched 0 rows and
    it died, and crash-resurrect relaunched it into the same window. The
    supervisor's session record IS the missing evidence, so the deadline consults
    it before giving up.

    The liveness question is also asked on a schedule now, not only at the
    deadline, because the child's own boot watchdog (`agent/_boot_deadline.py`)
    exits a boot that stops progressing — so a process that still exists is one
    that is still getting somewhere, and there is no longer any reason to sit out
    a window before believing it. Hence a dead child is caught in about one probe
    interval rather than at the deadline.

    Every test runs on a virtual clock over the PRODUCTION timeout constants: the
    numbers under test are the ones that ship. The poll interval is widened
    (virtual seconds are free, DB round-trips are not) — it only sets how finely
    the loop samples, never an outcome.
    """

    @staticmethod
    def _install(
        monkeypatch: pytest.MonkeyPatch, clock: _FakeClock, *, alive: bool
    ) -> dict[str, int]:
        """Point the module at `clock`, stub the liveness probe to `alive`, and
        return a call counter for that probe (the bound on extensions is
        observable as "asked at most once")."""
        probes = {"n": 0}

        def _probe(_agent_id: int) -> bool:
            probes["n"] += 1
            return alive

        monkeypatch.setattr("ops.agent_launch.time", clock)
        monkeypatch.setattr("ops.agent_launch._launched_process_alive", _probe)
        monkeypatch.setattr("ops.agent_launch._LAUNCH_CONFIRM_POLL_INTERVAL_SEC", 5.0)
        return probes

    def test_live_child_that_claims_late_is_confirmed_not_stolen(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The incident, inverted: the child claims after the normal deadline but
        while it is still alive, and the confirm now returns successfully instead
        of raising (which is what force-terminated the live child)."""
        agent_id = _spawn_agent()
        claim_at = boot_timing.LAUNCH_CONFIRM_TIMEOUT_SEC + 20.0
        assert claim_at < boot_timing.BOOT_REAP_GRACE_SEC, (
            "test premise: the claim must land inside the extension, not past it"
        )

        def _claim_when_due(now: float) -> None:
            if now < claim_at:
                return
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET status = 'running', pid = 4242 "
                    "WHERE id = %s AND status = 'idling'",
                    (agent_id,),
                )
            db_conn.commit()

        clock = _FakeClock(on_tick=_claim_when_due)
        probes = self._install(monkeypatch, clock, alive=True)

        _wait_for_agent_claim(agent_id)  # no raise

        assert clock.now > boot_timing.LAUNCH_CONFIRM_TIMEOUT_SEC, (
            "the wait must have been extended past the normal deadline, not merely "
            "have returned early"
        )
        assert probes["n"] >= 1, "the wait must have consulted the supervisor at all"
        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "running"

    def test_extension_is_bounded_by_the_reap_grace(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live child that never claims still fails — at the reap grace, after
        exactly ONE extension. Past that bound the restarter's dead-birth reaper
        owns the row, so waiting longer would be waiting on someone else's row."""
        agent_id = _spawn_agent()
        clock = _FakeClock()
        self._install(monkeypatch, clock, alive=True)

        with pytest.raises(RuntimeError, match=r"pid stayed NULL"):
            _wait_for_agent_claim(agent_id)

        # That the wait ENDED at the grace is what proves the extension is granted
        # once rather than renewed on every poll; a renewing extension would never
        # reach this bound at all. (Probe COUNT stopped being the observation when
        # liveness moved onto a schedule — it is now asked ~every second.)
        assert (
            boot_timing.BOOT_REAP_GRACE_SEC
            <= clock.now
            < boot_timing.BOOT_REAP_GRACE_SEC + agent_launch._LAUNCH_CONFIRM_POLL_INTERVAL_SEC
        ), f"waited {clock.now}s, expected the reap grace as the hard bound"
        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "idling", (
            "the confirm only raises; forcing 'terminated' is the caller's job"
        )

    def test_dead_child_fails_within_a_probe_interval_not_at_the_deadline(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A launch whose process is gone gets no patience at all — and no longer
        has to wait out the deadline to be told so.

        The original agent-137/44 case (spawn OK, child crashed on import) used to
        cost the full confirm window before anyone looked, purely because liveness
        was only consulted at the deadline. It is now consulted on a schedule, so
        the failure surfaces about one probe interval in. The same path catches a
        child that its own boot watchdog exited for stalling, which is what turns
        a wedged boot from "burns the whole extended window" into "fails as soon
        as it stops progressing"."""
        agent_id = _spawn_agent()
        clock = _FakeClock()
        probes = self._install(monkeypatch, clock, alive=False)

        with pytest.raises(RuntimeError, match=r"pid stayed NULL"):
            _wait_for_agent_claim(agent_id)

        assert probes["n"] >= 1
        assert clock.now < boot_timing.LAUNCH_CONFIRM_TIMEOUT_SEC, (
            f"a dead child must be caught by the scheduled liveness probe, not by the "
            f"{boot_timing.LAUNCH_CONFIRM_TIMEOUT_SEC}s deadline — waited {clock.now}s"
        )

    def test_a_child_that_claimed_then_exited_is_still_a_confirmed_start(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The race the scheduled probe opens, and its guard.

        A very fast agent can claim its row and exit within one probe interval, so
        "the process is gone" arrives while the row already says the launch
        succeeded. Failing on the probe alone would report a dead launch for an
        agent that ran; the wait therefore collapses its deadline and re-reads the
        row rather than raising where it stands. Multiplying the probe from once
        per launch to once per second is what makes this worth pinning."""
        agent_id = _spawn_agent()

        def _claim_then_vanish(_agent_id: int) -> bool:
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET status = 'terminated', pid = 4242 "
                    "WHERE id = %s AND status = 'idling'",
                    (agent_id,),
                )
            db_conn.commit()
            return False

        clock = _FakeClock()
        self._install(monkeypatch, clock, alive=False)
        monkeypatch.setattr("ops.agent_launch._launched_process_alive", _claim_then_vanish)

        _wait_for_agent_claim(agent_id)  # no raise: it did start

        assert clock.now < boot_timing.LAUNCH_CONFIRM_TIMEOUT_SEC


@pytest.mark.real_agent_launch
def test_launch_hands_the_child_the_window_it_will_be_judged_by(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam between the two halves of the boot deadline.

    The child cannot read this from `shared.config`: importing that module is a
    measurable slice of the very segment its watchdog has to cover, so the value
    can only arrive on argv, which is readable before any import. That makes the
    handoff itself the contract — a child launched without the flag arms nothing
    and silently reverts the launcher's liveness probe to the guess it used to
    be, with no error anywhere to say so.

    argv rather than the env dict is safe here because a timeout is not secret
    material; `tests/shared/test_no_secrets_on_argv.py` owns the values for which
    that distinction matters."""
    captured: list[list[str]] = []

    class _FakeSupervisor:
        @staticmethod
        def new_session(_name, argv, _cwd, **_kw) -> bool:
            captured.append([str(a) for a in argv])  # pyright: ignore[reportUnknownArgumentType]
            return True

        @staticmethod
        def kill_session(*_a, **_kw) -> tuple[bool, str]:
            return (True, "noop")

    monkeypatch.setattr("ops.agent_launch.native_proc", lambda: _FakeSupervisor)
    monkeypatch.setattr("ops.agent_launch.agent_spawn_env_dict", dict)

    agent_launch._launch_agent_process(11, confirm=False)

    argv = captured[0]
    for flag, expected in (
        ("--boot-stall-seconds", boot_timing.BOOT_STALL_SEC),
        ("--boot-budget-seconds", boot_timing.BOOT_BUDGET_SEC),
    ):
        assert flag in argv, (
            f"without {flag} the child arms that bound not at all, and nothing fails loudly"
        )
        assert float(argv[argv.index(flag) + 1]) == expected, (
            "the launcher must judge the child by the same windows it handed it"
        )


class TestLaunchRetry:
    """`_launch_or_force_terminated` retries transient launch failures, only force-terminating when exhausted.
    Covers the root-cause fix after ava.self.update() where an agent would become terminated:
    a single launch jitter no longer kills the agent outright.
    `_launch_agent_process` / `_require_released_agent_session` are monkeypatched in this class (no real process),
    and the backoff base is patched to 0 for speed."""

    def test_retry_then_succeed_does_not_terminate(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """First two launches raise RuntimeError, third succeeds -> agent is not force-terminated;
        each retry clears a stale session beforehand. Verify that transient launch jitter can self-heal."""
        agent_id = _spawn_agent()  # create an 'idling' row for observation
        attempts = {"n": 0}
        kills = {"n": 0}

        def flaky(_id: int, **_kw: object) -> None:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError(f"transient launch fail #{attempts['n']}")

        monkeypatch.setattr("ops.agent_launch._LAUNCH_RETRY_BASE_BACKOFF_SEC", 0.0)
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", flaky)
        monkeypatch.setattr(
            "ops.agent_launch._require_released_agent_session",
            lambda _id: kills.__setitem__("n", kills["n"] + 1),  # pyright: ignore[reportUnknownArgumentType]
        )

        _launch_or_force_terminated(agent_id)

        assert attempts["n"] == 3, "should retry until the third succeeds"
        assert kills["n"] == 2, "each retry clears a stale session once"
        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] != "terminated"

    def test_exhaust_retries_forces_terminated(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """launch continuously raises RuntimeError -> after MAX_RETRIES+1 attempts, force-terminate + re-raise."""
        agent_id = _spawn_agent()
        attempts = {"n": 0}

        def always_boom(_id: int, **_kw: object) -> None:
            attempts["n"] += 1
            raise RuntimeError("permanent launch fail")

        monkeypatch.setattr("ops.agent_launch._LAUNCH_RETRY_BASE_BACKOFF_SEC", 0.0)
        monkeypatch.setattr("ops.agent_launch._LAUNCH_MAX_RETRIES", 3)
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", always_boom)
        monkeypatch.setattr("ops.agent_launch._require_released_agent_session", lambda _id: None)  # pyright: ignore[reportUnknownArgumentType]

        with pytest.raises(RuntimeError, match="permanent launch fail"):
            _launch_or_force_terminated(agent_id)

        assert attempts["n"] == 4, "1 initial + 3 retries"
        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "terminated"

    def test_exhausted_retries_do_not_clobber_a_claimed_row(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The child claims late — between the last attempt's deadline and the
        force-terminate write — and the row it now owns must survive.

        "Every attempt failed" is only the launcher's local view: each attempt
        left a process behind, and one of them can reach `claim_agent_row`
        a moment after the launcher stopped waiting. This write used to carry no
        status predicate, so it buried that live agent under 'terminated' — and
        crash-resurrect, which claims exactly this `termination_source`, then
        launched a SECOND process for an agent that was already up. The re-raise
        still happens (the launch genuinely did not confirm); only the write is
        withheld."""
        agent_id = _spawn_agent()

        def always_boom(_id: int, **_kw: object) -> None:
            raise RuntimeError("confirm timed out while the child was still importing")

        monkeypatch.setattr("ops.agent_launch._LAUNCH_RETRY_BASE_BACKOFF_SEC", 0.0)
        monkeypatch.setattr("ops.agent_launch._LAUNCH_MAX_RETRIES", 1)
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", always_boom)

        # The late claim: the child wins the row while the launcher is between its
        # last failed attempt and its cleanup write.
        def _claim_then_note(_id: int) -> None:
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET status = 'running', pid = 777 "
                    "WHERE id = %s AND status = 'idling'",
                    (agent_id,),
                )
            db_conn.commit()

        monkeypatch.setattr("ops.agent_launch._require_released_agent_session", _claim_then_note)

        with pytest.raises(RuntimeError, match="confirm timed out"):
            _launch_or_force_terminated(agent_id)

        row = _agents_row(db_conn, agent_id)
        assert row is not None and (row[2], row[3]) == ("running", 777), (
            "the force-terminate write must be guarded on status='idling' — a child "
            f"that claimed late owns this row, got {row}"
        )

    def test_non_runtimeerror_propagates_without_force_terminate(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-RuntimeError is an unexpected bug: immediately propagate upward, no retry and no force-terminate
        (fail fast, leave state for investigation)."""
        agent_id = _spawn_agent()
        attempts = {"n": 0}

        def boom_value(_id: int, **_kw: object) -> None:
            attempts["n"] += 1
            raise ValueError("unexpected bug")

        monkeypatch.setattr("ops.agent_launch._LAUNCH_RETRY_BASE_BACKOFF_SEC", 0.0)
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", boom_value)
        monkeypatch.setattr("ops.agent_launch._require_released_agent_session", lambda _id: None)  # pyright: ignore[reportUnknownArgumentType]

        with pytest.raises(ValueError, match="unexpected bug"):
            _launch_or_force_terminated(agent_id)

        assert attempts["n"] == 1, "non-RuntimeError does not retry"
        row = _agents_row(db_conn, agent_id)
        assert row is not None and row[2] == "idling", "non-RuntimeError does not force-terminate"


def _fake_launch_supervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake the native supervisor so `_launch_agent_process` starts no real
    process: `new_session` returns True, `kill_session` is a noop."""

    class _FakeSupervisor:
        @staticmethod
        def new_session(*_a, **_kw):
            return True

        @staticmethod
        def kill_session(*_a, **_kw):
            return (True, "noop")

    monkeypatch.setattr("ops.agent_launch.native_proc", lambda: _FakeSupervisor)


@pytest.mark.real_agent_launch
class TestStderrLogsDir:
    """`_launch_agent_process` redirects the child python's stderr to
    `{LOGS_DIR}/agent-N.stderr.log` (#51, agent 152 death investigation). On a fresh machine /
    fresh CI runner, if LOGS_DIR does not exist, spawn would fail → spawn 500 → e2e fixture
    (POST /api/agents) all ERROR (#52 / #54 PR e2e fail exposed).

    Regression prevention: _launch_agent_process must idempotently mkdir LOGS_DIR, and must not rely on
    shared/log.py sinks to create it beforehand (the gateway startup path may not exercise those sinks).
    """

    def test_mkdir_creates_logs_dir_when_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """LOGS_DIR points to a non-existent subdirectory; after calling _launch_agent_process, the directory
        must exist (idempotent mkdir). Fake native supervisor + noop confirm."""
        from ops.agent_launch import _launch_agent_process
        from shared.config import settings
        from shared.envfile import upsert_env

        fresh_home = tmp_path / "fresh"
        fresh_logs_dir = fresh_home / "logs"
        assert not fresh_logs_dir.exists()
        monkeypatch.setattr(settings.general, "ava_home", fresh_home)
        upsert_env(fresh_home / ".env", {"AVA_RUNNER_DB_PASSWORD": "abc"})

        # let _wait_for_agent_claim return immediately (avoids needing real agents_meta table)
        monkeypatch.setattr(
            "ops.agent_launch._wait_for_agent_claim",
            lambda _id, _attempt: None,  # pyright: ignore[reportUnknownArgumentType]
        )
        _fake_launch_supervisor(monkeypatch)  # don't actually start a process
        monkeypatch.setattr("ops.agent_launch.agent_spawn_env_dict", dict)

        _launch_agent_process(99999)  # agent_id arbitrary, doesn't read DB
        assert fresh_logs_dir.exists(), (
            "_launch_agent_process must idempotently mkdir LOGS_DIR — "
            "the parent directory of the redirected stderr file is not auto-created"
        )

    def test_mkdir_idempotent_when_dir_already_exists(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        unit_home: Path,
    ) -> None:
        """When LOGS_DIR already exists, does not raise (mkdir exist_ok=True)."""
        from ops.agent_launch import _launch_agent_process
        from shared.envfile import upsert_env

        existing = tmp_path / "logs"
        existing.mkdir()
        upsert_env(unit_home / ".env", {"AVA_RUNNER_DB_PASSWORD": "abc"})
        monkeypatch.setattr(
            "ops.agent_launch._wait_for_agent_claim",
            lambda _id, _attempt: None,  # pyright: ignore[reportUnknownArgumentType]
        )
        _fake_launch_supervisor(monkeypatch)
        monkeypatch.setattr("ops.agent_launch.agent_spawn_env_dict", dict)
        _launch_agent_process(0)  # does not raise


def _insert_checkpoint(
    db: psycopg.Connection,
    agent_id: int,
    ckpt_id: str,
    parent_id: str | None = None,
) -> None:
    """Test helper: directly INSERT a LangGraph checkpoint row (simplified version, bypassing
    PostgresSaver's serialization overhead). The fork copy logic only cares about SQL row-level copy + chain
    integrity, not the real content of the checkpoint blob."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_id, parent_checkpoint_id, "
            "checkpoint, metadata) VALUES (%s, %s, %s, '{}'::jsonb, '{}'::jsonb)",
            (str(agent_id), ckpt_id, parent_id),
        )
    db.commit()


def _checkpoint_ids(db: psycopg.Connection, agent_id: int) -> list[str]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id = %s ORDER BY checkpoint_id",
            (str(agent_id),),
        )
        return [r[0] for r in cur.fetchall()]


class TestSpawnFork:
    def test_fork_copies_target_and_ancestor_chain(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fork copies target ckpt + all ancestors (recursively via parent_checkpoint_id),
        so the new agent sees the full history."""
        source = _spawn_agent()
        # construct chain: a (root) → b → c
        _insert_checkpoint(db_conn, source, "a-ckpt", parent_id=None)
        _insert_checkpoint(db_conn, source, "b-ckpt", parent_id="a-ckpt")
        _insert_checkpoint(db_conn, source, "c-ckpt", parent_id="b-ckpt")

        new_id = _spawn_agent(fork_from=source, fork_checkpoint="c-ckpt")

        # new agent gets all three a/b/c
        assert sorted(_checkpoint_ids(db_conn, new_id)) == ["a-ckpt", "b-ckpt", "c-ckpt"]
        # source is untouched
        assert sorted(_checkpoint_ids(db_conn, source)) == ["a-ckpt", "b-ckpt", "c-ckpt"]

    def test_fork_does_not_copy_descendants_after_target(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fork at b → new agent only gets a + b, not c (checkpoints after b).
        Verify the semantic: "fork cuts the state before the fork point"."""
        source = _spawn_agent()
        _insert_checkpoint(db_conn, source, "a", parent_id=None)
        _insert_checkpoint(db_conn, source, "b", parent_id="a")
        _insert_checkpoint(db_conn, source, "c", parent_id="b")

        new_id = _spawn_agent(fork_from=source, fork_checkpoint="b")

        assert sorted(_checkpoint_ids(db_conn, new_id)) == ["a", "b"]

    def test_fork_records_source_in_agents_row(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fork_source_agent_id + fork_source_checkpoint_id are written to the agents row."""
        source = _spawn_agent()
        _insert_checkpoint(db_conn, source, "x")

        new_id = _spawn_agent(fork_from=source, fork_checkpoint="x")

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT fork_source_agent_id, fork_source_checkpoint_id FROM agents_meta WHERE id = %s",
                (new_id,),
            )
            row = cur.fetchone()
        assert row == (source, "x")

    def test_fork_copies_blobs_for_source_agent(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """checkpoint_blobs are fully copied from the source agent — the actual message data is in blobs."""
        source = _spawn_agent()
        _insert_checkpoint(db_conn, source, "ck")
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type, blob) "
                "VALUES (%s, '', 'messages', '1', 'msgpack', %s)",
                (str(source), b"\xde\xad\xbe\xef"),
            )
        db_conn.commit()

        new_id = _spawn_agent(fork_from=source, fork_checkpoint="ck")

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT channel, version, blob FROM checkpoint_blobs WHERE thread_id = %s",
                (str(new_id),),
            )
            rows = cur.fetchall()
        assert rows == [("messages", "1", b"\xde\xad\xbe\xef")]

    def test_fork_unknown_checkpoint_raises_and_rolls_back(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fork_checkpoint does not exist in source → raise + transaction rollback (no orphan agents /
        agents_meta rows)."""
        source = _spawn_agent()
        # intentionally do not INSERT any checkpoint

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM agents_meta")
            row = cur.fetchone()
            assert row is not None
            count_before = row[0]

        with pytest.raises(ForkCheckpointNotFound, match="bogus-ckpt"):
            _spawn_agent(fork_from=source, fork_checkpoint="bogus-ckpt")

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM agents_meta")
            row = cur.fetchone()
            assert row is not None
            assert row[0] == count_before  # no new agents row

    def test_fork_from_without_checkpoint_raises_value_error(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """fork_from / fork_checkpoint must be provided as a pair."""
        with pytest.raises(ValueError, match="must be provided as a pair"):
            _spawn_agent(fork_from=1)
        with pytest.raises(ValueError, match="must be provided as a pair"):
            _spawn_agent(fork_checkpoint="x")

    def test_fork_inserts_identity_inbound(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fork inserts a kind='fork' lifecycle inbound (source='agent:{fork_from}', content='')
        within the same transaction that copies the checkpoint, so the new process receives
        the identity marker on its first claim. The insert is committed before _launch_agent_process."""
        source = _spawn_agent()
        _insert_checkpoint(db_conn, source, "ck")

        new_id = _spawn_agent(fork_from=source, fork_checkpoint="ck")

        assert _inbound_rows(db_conn, new_id) == [("", "fork", f"agent:{source}")]

    def test_plain_spawn_inserts_no_inbound(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-fork spawn does not insert any inbound (the agent starts from nothing, no 'who am I' to correct)."""
        new_id = _spawn_agent()
        assert _inbound_count(db_conn, new_id) == 0

    def test_fork_prompt_committed_before_launch(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fork + prompt: the prompt chat inbound must be committed before _launch_agent_process
        so that it lands together with the fork marker in the forked agent's first claim batch
        (otherwise the agent would first run a turn based on inherited history + fork marker,
        and the prompt would arrive in the next batch, which is logically wrong).
        Snapshot inbound rows from the mocked launch to prove: at launch time,
        [fork marker, prompt] are both in the DB, correctly ordered (marker in the main transaction,
        prompt in a separate subsequent insert)."""
        source = _spawn_agent()
        _insert_checkpoint(db_conn, source, "ck")

        seen: list[tuple] = []

        def _spy_launch(agent_id: int, config_overlay: dict | None = None, **_kw) -> None:
            with shared.db.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT content, kind, source FROM inbound_messages "
                    "WHERE agent_id = %s ORDER BY id ASC",
                    (agent_id,),
                )
                seen.extend(cur.fetchall())  # pyright: ignore[reportUnknownMemberType]

        monkeypatch.setattr("ops.agent_launch._launch_agent_process", _spy_launch)  # pyright: ignore[reportUnknownArgumentType]

        _spawn_agent(fork_from=source, fork_checkpoint="ck", prompt="go do X", prompt_source="user")

        assert seen == [("", "fork", f"agent:{source}"), ("go do X", "chat", "user")]

    def test_spawn_prompt_pairing_validated(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """prompt / prompt_source must be provided as a pair."""
        with pytest.raises(ValueError, match="prompt and prompt_source must be provided as a pair"):
            _spawn_agent(prompt="hi")
        with pytest.raises(ValueError, match="prompt and prompt_source must be provided as a pair"):
            _spawn_agent(prompt_source="user")

    def test_fork_event_target_is_fork_source_not_executor(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """User ruling 2026-08-28 (task #1879): the fork event's
        target_agent_id is the fork SOURCE (the lineage parent), never the
        executor who triggered the fork; the executor stays in `source`. The
        agents_meta spawner column records the fork source the same way (it
        is the frontend tree's parent fallback)."""
        import json
        from datetime import UTC, datetime

        from shared import telemetry
        from shared.paths import logs_dir

        source = _spawn_agent()
        executor = _spawn_agent()
        _insert_checkpoint(db_conn, source, "ck")

        new_id = _spawn_agent(spawner=f"agent:{executor}", fork_from=source, fork_checkpoint="ck")

        telemetry.sync()
        day = datetime.now(UTC).strftime("%Y%m%d")
        path = logs_dir() / f"events-{day}.jsonl"
        fork_rows: list[dict[str, Any]] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("event_name") != "fork" or obj.get("agent_id") != new_id:
                    continue
                fork_rows.append(obj)
        assert len(fork_rows) == 1
        assert fork_rows[0]["source"] == f"agent:{executor}"
        assert fork_rows[0]["target_agent_id"] == source
        assert fork_rows[0]["attributes"]["fork_from"] == source

        with db_conn.cursor() as cur:
            cur.execute("SELECT spawner, born_spawner FROM agents_meta WHERE id = %s", (new_id,))
            row = cur.fetchone()
        assert row is not None
        assert row == (f"agent:{source}", f"agent:{source}")

    def test_spawn_event_target_is_spawner(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plain spawn keeps the old direction: target_agent_id = the
        spawner (its lineage parent), and the spawner column records it
        verbatim — the fork-source override must not leak into spawns."""
        import json
        from datetime import UTC, datetime

        from shared import telemetry
        from shared.paths import logs_dir

        parent = _spawn_agent()
        new_id = _spawn_agent(spawner=f"agent:{parent}")

        telemetry.sync()
        day = datetime.now(UTC).strftime("%Y%m%d")
        path = logs_dir() / f"events-{day}.jsonl"
        spawn_rows: list[dict[str, Any]] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("event_name") != "spawn" or obj.get("agent_id") != new_id:
                    continue
                spawn_rows.append(obj)
        assert len(spawn_rows) == 1
        assert spawn_rows[0]["source"] == f"agent:{parent}"
        assert spawn_rows[0]["target_agent_id"] == parent

        with db_conn.cursor() as cur:
            cur.execute("SELECT spawner, born_spawner FROM agents_meta WHERE id = %s", (new_id,))
            row = cur.fetchone()
        assert row is not None
        assert row == (f"agent:{parent}", f"agent:{parent}")

    def test_fork_inbound_via_unified_path_emits_fork_source_target(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The unified inbound writer's kind='fork' mapping (currently
        reached by no caller; the fork inbound is inserted with raw SQL in
        create_agent_row) must emit the same direction: target = the fork
        source parsed from the "agent:{fork_source}" identity marker, never
        None — a latent wrong-target path for the same ruling."""
        import json
        from datetime import UTC, datetime

        from shared import telemetry
        from shared.paths import logs_dir

        source = _spawn_agent()
        new_id = _spawn_agent()
        shared.db.insert_inbound_message(db_conn, new_id, "", source=f"agent:{source}", kind="fork")

        telemetry.sync()
        day = datetime.now(UTC).strftime("%Y%m%d")
        path = logs_dir() / f"events-{day}.jsonl"
        fork_rows: list[dict[str, Any]] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("event_name") != "fork" or obj.get("agent_id") != new_id:
                    continue
                fork_rows.append(obj)
        # The event carries the inbound id in its payload — the raw-SQL fork
        # path emits no such event, so exactly this one row is expected.
        assert len(fork_rows) == 1
        assert fork_rows[0]["target_agent_id"] == source
        assert fork_rows[0]["source"] == f"agent:{source}"


@pytest.mark.real_agent_launch
class TestEnvForward:
    """`_launch_agent_process` builds the detached child's env from the current
    process's env (agent_spawn_env_dict) — so e2e / multi-instance / debug
    scenarios that set AVA_DB_URL / AVA_LLM_OVERRIDE etc. reach the agent
    subprocess (the child inherits it directly; no server env to freeze a stale
    env). Dropping the cluster-common secrets so a restart re-fetches is covered by
    tests/gateway/test_spawn_env_forwarding.py.
    """

    @staticmethod
    def _capture_launch_env(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        captured: list[dict] = []

        class _FakeSupervisor:
            @staticmethod
            def new_session(_name, _argv, _cwd, *, env, **_kw):
                captured.append(dict(env))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
                return True

            @staticmethod
            def kill_session(*_a, **_kw):
                return (True, "noop")

        monkeypatch.setattr("ops.agent_launch.native_proc", lambda: _FakeSupervisor)
        # spawn launches with confirm=False, so it captures the env and returns
        # without polling — no confirm timeout to shrink here.
        return captured

    def test_forwards_ava_env_to_child(
        self,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured = self._capture_launch_env(monkeypatch)  # pyright: ignore[reportUnknownMemberType]
        monkeypatch.setattr("shared.bootstrap.config_source_is_local", lambda: False)
        # An agent-scope allowlist key rides through (the child needs it before
        # Settings); a non-modeled AVA_* knob does NOT (F-s3-4: the allowlist
        # is positive — AVA_AGENT_ID and friends never inherit). Replace
        # os.environ wholesale (the spawn env dict reads the live env directly,
        # not Settings — monkeypatch.setenv would be lint-banned and wouldn't
        # model the live-env read anyway).
        monkeypatch.setattr(
            os,
            "environ",
            {
                **os.environ,
                "AVA_DB_URL": "postgresql://ava_runner:bootstrap-password@localhost/ava",
                "AVA_LLM_OVERRIDE": "mod:factory",
                "AVA_FOO_E2E": "bar",
            },
        )

        _spawn_agent()  # confirm=False — returns after the launch

        assert len(captured) == 1  # pyright: ignore[reportUnknownArgumentType]
        env = captured[0]
        assert env["AVA_LLM_OVERRIDE"] == "mod:factory"
        assert "AVA_FOO_E2E" not in env


class TestRespawnConfigOverlay:
    """`respawn_agent`'s launch overlay source is the agents_meta.config_overlay column
    (authoritative), **not** the restart inbound payload. The restart inbound's
    payload only serves as an audit trail for the lifecycle marker, passed through into restart_completed,
    and does not drive the launch overlay. This class locks the invariant that "payload does not pollute the launch path":
    when the column is NULL (default), launch receives None regardless of what the payload contains.
    """

    @staticmethod
    def _setup_restarting_with_payload(
        db: psycopg.Connection,
        payload: dict | None,
    ) -> int:
        """spawn → simulate 'restarting' + restart inbound (with payload)."""
        agent_id = _spawn_agent()
        with db.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'restarting' WHERE id = %s", (agent_id,))
            import json as _json

            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, status, payload) "
                "VALUES (%s, '', 'restart', 'self', 'done', %s::jsonb)",
                (agent_id, _json.dumps(payload) if payload is not None else None),
            )
        db.commit()
        return agent_id

    def test_respawn_passes_config_overlay_to_launch(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """agents_meta.config_overlay column → _launch_or_force_terminated receives
        config_overlay=that dict (via argparse → apply_config_overlay to change child behavior).
        The restart inbound payload carries the same overlay for audit trail, but the
        launch source is the authoritative column.
        """
        overlay = {"some_plugin": {"enabled": True}, "max_turns": 5}
        captured: list[dict | None] = []

        def fake_launch(
            _id: int,
            *,
            config_overlay: dict | None = None,
            birth_config: dict | None = None,
        ) -> None:
            captured.append(config_overlay)  # pyright: ignore[reportUnknownMemberType]

        agent_id = self._setup_restarting_with_payload(db_conn, payload={"config_overlay": overlay})  # pyright: ignore[reportUnknownMemberType]
        # Set config_overlay column — this is the authoritative source after refactor
        import json as _json

        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET config_overlay = %s::jsonb WHERE id = %s",
                (_json.dumps(overlay), agent_id),
            )
        db_conn.commit()
        # _setup monkeypatched _launch_agent_process with lambda, now replace with capture version
        # _launch_or_force_terminated (respawn calls that wrapper)
        monkeypatch.setattr("ops.agent_launch._launch_or_force_terminated", fake_launch)  # pyright: ignore[reportUnknownArgumentType]

        respawn_agent(agent_id)

        assert captured == [overlay], (
            f"config_overlay was not forwarded to _launch_or_force_terminated; got: {captured!r}"
        )

    def test_respawn_passes_none_when_payload_missing(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """payload=NULL and column is NULL (default) → launch receives config_overlay=None.
        Locks the "plain restart without config" path, ensuring it still correctly carries no overlay
        (column is source, payload does not drive launch)."""
        captured: list[dict | None] = []

        def fake_launch(
            _id: int,
            *,
            config_overlay: dict | None = None,
            birth_config: dict | None = None,
        ) -> None:
            captured.append(config_overlay)  # pyright: ignore[reportUnknownMemberType]

        agent_id = self._setup_restarting_with_payload(db_conn, payload=None)  # pyright: ignore[reportUnknownMemberType]
        monkeypatch.setattr("ops.agent_launch._launch_or_force_terminated", fake_launch)  # pyright: ignore[reportUnknownArgumentType]

        respawn_agent(agent_id)

        assert captured == [None]

    def test_respawn_passes_none_when_payload_lacks_config_overlay_key(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """payload is dict containing arbitrary keys but column is NULL → launch receives config_overlay=None.
        Locks that "payload content (with or without a config_overlay key) does not drive launch overlay,
        column is the authoritative source"."""
        captured: list[dict | None] = []

        def fake_launch(
            _id: int,
            *,
            config_overlay: dict | None = None,
            birth_config: dict | None = None,
        ) -> None:
            captured.append(config_overlay)  # pyright: ignore[reportUnknownMemberType]

        agent_id = self._setup_restarting_with_payload(db_conn, payload={"other_field": "value"})  # pyright: ignore[reportUnknownMemberType]
        monkeypatch.setattr("ops.agent_launch._launch_or_force_terminated", fake_launch)  # pyright: ignore[reportUnknownArgumentType]

        respawn_agent(agent_id)

        assert captured == [None]

    def test_respawn_inserts_restart_completed_with_full_payload(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The payload column of the restart_completed inbound must == the original 'restart' inbound payload
        (transparent pass-through, so the new process can read config_overlay and write effective_config when it starts).
        If broken, the audit trail loses a link, making it impossible to trace the config intent at restart time."""
        overlay = {"foo": "bar"}
        original = {"config_overlay": overlay, "extra": 42}
        agent_id = self._setup_restarting_with_payload(db_conn, payload=original)  # pyright: ignore[reportUnknownMemberType]

        respawn_agent(agent_id)

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT payload FROM inbound_messages "
                "WHERE agent_id = %s AND kind = 'restart_completed'",
                (agent_id,),
            )
            row = cur.fetchone()
        assert row is not None and row[0] == original


class TestRespawnResurrectColumnOverlay:
    """respawn_agent and resurrect_agent must read config_overlay from the
    agents_meta column (the authoritative store), not from the restart inbound
    payload. Tasks 4-5 established the column as the source of truth; this class
    locks the launch path to that source for both wake-up paths.
    """

    def test_respawn_reads_config_overlay_from_column_not_payload(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """respawn_agent reads config_overlay from agents_meta column.

        The restart inbound has payload=NULL (no config in payload) to prove the
        column — not the payload — is the source of the launch overlay.
        """
        import json as _json

        agent_id = _spawn_agent()
        # Set the column explicitly — this is the authoritative source
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'restarting', "
                "config_overlay = %s::jsonb WHERE id = %s",
                (_json.dumps({"llm_model": "gemini-3.1-pro-preview"}), agent_id),
            )
            # restart inbound with payload=NULL — deliberately no config here
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
                "VALUES (%s, '', 'restart', 'self', 'done')",
                (agent_id,),
            )
        db_conn.commit()

        captured: list[dict | None] = []

        def fake_launch(
            _id: int,
            *,
            config_overlay: dict | None = None,
            birth_config: dict | None = None,
        ) -> None:
            captured.append(config_overlay)  # pyright: ignore[reportUnknownMemberType]

        monkeypatch.setattr("ops.agent_launch._launch_or_force_terminated", fake_launch)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr("ops.agent_launch._require_released_agent_session", lambda _id: None)  # pyright: ignore[reportUnknownArgumentType]

        result = respawn_agent(agent_id)

        assert result is True
        assert captured == [{"llm_model": "gemini-3.1-pro-preview"}], (
            f"respawn_agent must read config_overlay from column, not payload; got: {captured!r}"
        )

    def test_resurrect_reads_config_overlay_from_column(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resurrect_agent passes config_overlay from agents_meta column to launch."""
        import json as _json

        agent_id = _spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'terminated', "
                "config_overlay = %s::jsonb WHERE id = %s",
                (_json.dumps({"llm_model": "claude-opus-4-8"}), agent_id),
            )
        db_conn.commit()

        captured: list[dict | None] = []

        def fake_launch(
            _id: int,
            *,
            config_overlay: dict | None = None,
            birth_config: dict | None = None,
            confirm: bool = True,
            resurrect_attempt: tuple[int, int, float] | None = None,
        ) -> None:
            captured.append(config_overlay)  # pyright: ignore[reportUnknownMemberType]

        monkeypatch.setattr("ops.agent_launch._launch_agent_process", fake_launch)  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr("ops.agent_launch._require_released_agent_session", lambda _id: None)  # pyright: ignore[reportUnknownArgumentType]

        resurrect_agent(agent_id, resurrected_by="user", prompt="test")

        assert captured == [{"llm_model": "claude-opus-4-8"}], (
            f"resurrect_agent must pass config_overlay from column to launch; got: {captured!r}"
        )


@pytest.mark.real_agent_launch
class TestLaunchAgentProcessConfigOverlay:
    """When config_overlay is not None, `_launch_agent_process` serializes JSON into
    the child's **environment** (`$AVA_AGENT_CONFIG_OVERLAY`), never its argv: an
    overlay may set any Settings field, a provider api_key included, and `ps`
    shows argv to any local user (issue #974). A child environment is owner-only.
    With no overlay the variable is absent (clean env, `if config_overlay:` falsy
    in the child).
    """

    @staticmethod
    def _capture_launch(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, str]]]:
        captured: list[tuple[list[str], dict[str, str]]] = []
        monkeypatch.setattr("ops.agent_launch.agent_spawn_env_dict", dict)

        class _FakeSupervisor:
            @staticmethod
            def new_session(_name, argv, _cwd, *, env, **_kw):
                captured.append((list(argv), dict(env)))  # pyright: ignore[reportUnknownArgumentType]
                return True

            @staticmethod
            def kill_session(*_a, **_kw):
                return (True, "noop")

        monkeypatch.setattr("ops.agent_launch.native_proc", lambda: _FakeSupervisor)
        monkeypatch.setattr(
            "ops.agent_launch._wait_for_agent_claim",
            lambda _id, _attempt: None,  # pyright: ignore[reportUnknownArgumentType]
        )
        return captured

    def test_overlay_rides_the_child_env_not_argv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """config_overlay={...} → the JSON is in the child env under
        AVA_AGENT_CONFIG_OVERLAY and nowhere on argv. If broken, the child never
        applies the overlay at all."""
        import json as _json

        from ops.agent_launch import _launch_agent_process

        captured = self._capture_launch(monkeypatch)
        overlay: dict[str, object] = {"plugin_a": True, "plugin_b": {"x": 1}}

        _launch_agent_process(7, config_overlay=overlay)

        argv, env = captured[0]
        # sort_keys → deterministic serialization
        assert env[AGENT_CONFIG_OVERLAY_ENV] == _json.dumps(overlay, sort_keys=True)
        assert "--config-overlay" not in argv
        assert not any("plugin_a" in arg for arg in argv)

    def test_no_overlay_omits_the_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """config_overlay=None (default) → the child env carries no overlay var."""
        from ops.agent_launch import _launch_agent_process

        captured = self._capture_launch(monkeypatch)

        _launch_agent_process(8)  # default config_overlay=None

        argv, env = captured[0]
        assert AGENT_CONFIG_OVERLAY_ENV not in env
        assert "--config-overlay" not in argv

    def test_overlay_with_special_chars_survives_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Overlay JSON containing spaces/quotes goes through the env verbatim — no
        shell, no quoting, nothing to split it. This is the whole class of quoting
        hazards a command line would reintroduce."""
        import json as _json

        from ops.agent_launch import _launch_agent_process

        captured = self._capture_launch(monkeypatch)
        overlay: dict[str, object] = {"key with space": "value's"}

        _launch_agent_process(9, config_overlay=overlay)

        _argv, env = captured[0]
        assert env[AGENT_CONFIG_OVERLAY_ENV] == _json.dumps(overlay, sort_keys=True)

    def test_a_secret_bearing_overlay_never_reaches_argv(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reason this moved: an overlay may legitimately set an api_key field."""
        from ops.agent_launch import _launch_agent_process

        captured = self._capture_launch(monkeypatch)

        _launch_agent_process(10, config_overlay={"deepseek_api_key": "sk-top-secret"})

        argv, env = captured[0]
        assert not any("sk-top-secret" in arg for arg in argv)
        assert "sk-top-secret" in env[AGENT_CONFIG_OVERLAY_ENV]


@pytest.mark.real_agent_launch
class TestLaunchConfirmActuallyWaits:
    """The deadline calculation in `_wait_for_agent_claim` must be
    `monotonic() + timeout` (future-oriented), not `monotonic() - timeout` (past-oriented).

    The `-` mutation would make the deadline always in the past → immediately hit timeout raise
    after the first poll, giving the child process no chance to start. In production it manifests as:
    every spawn / resurrect / respawn raises, because the child needs ~1-2s to import, but the function
    gives up in the first second.

    This test simulates "child UPDATEs status after the first poll but before the timeout";
    the correct `+ timeout` implementation waits for the child to start and returns normally;
    the `- timeout` mutation would raise immediately (didn't wait).
    """

    @pytest.mark.flaky  # real _time.sleep bg thread + wall-clock elapsed assertions
    def test_wait_loops_until_pid_is_claimed(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """child UPDATEs status='running' after ~150ms (later than the first poll but before the
        timeout) → the function should wait until that moment and return normally.
        """
        import threading
        import time as _time

        from ops.agent_launch import _wait_for_agent_claim

        # INSERT an 'idling' row directly — bypassing the helper's launch
        with db_conn.cursor() as cur:
            cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
            row = cur.fetchone()
            assert row is not None
            agent_id: int = row[0]
            cur.execute(
                "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'idling')",
                (agent_id,),
            )
        db_conn.commit()

        # timeout=10s (far above 150ms delay), poll_interval=50ms. The wide timeout distinguishes
        # from the upper bound in the assertion below (elapsed < 8.0). A slow SELECT on a loaded runner
        # may stall 3-4s (observed 3.45s once), the function still returns at status flip,
        # still far below 8.0; a real bug (waiting full timeout) would be ~10s, still discernible.
        monkeypatch.setattr("ops.agent_launch.LAUNCH_CONFIRM_TIMEOUT_SEC", 6.0)
        monkeypatch.setattr("ops.agent_launch._LAUNCH_CONFIRM_POLL_INTERVAL_SEC", 0.05)

        def delayed_running():
            _time.sleep(0.15)
            with (
                psycopg.connect(
                    __import__("shared.config", fromlist=["settings"]).settings.data_plane.db_url
                ) as c,
                c.cursor() as cur,
            ):
                cur.execute(
                    "UPDATE agents_meta SET status = 'running', pid = 4242 WHERE id = %s",
                    (agent_id,),
                )
                c.commit()

        threading.Thread(target=delayed_running, daemon=True).start()

        start = _time.monotonic()
        _wait_for_agent_claim(agent_id)  # should return normally
        elapsed = _time.monotonic() - start

        # Must wait for the child to start (~150ms), not return immediately (deadline should not be in the past)
        assert elapsed >= 0.1, (
            f"_wait_for_agent_claim did not actually wait for the child to start; "
            f"elapsed={elapsed:.3f}s — deadline calculation may be inverted (- instead of +)"
        )
        # Should not wait full timeout (returned when child started; upper bound gives generous headroom,
        # just needs to be reliably below the 10s timeout to prove return was due to status flip, not timeout)
        assert elapsed < 4.5, (
            f"_wait_for_agent_claim waited too long (expected ~150-200ms); elapsed={elapsed:.3f}s"
        )


def _status(db_conn: psycopg.Connection, agent_id: int) -> str | None:
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    return row[0] if row else None


def _set_status(db_conn: psycopg.Connection, agent_id: int, status: str) -> None:
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = %s WHERE id = %s", (status, agent_id))
        # fail-fast: the row must exist and actually flip. A silent 0-row UPDATE
        # would otherwise surface much later as a baffling `assert <spawn-value> ==
        # <intended>` in the test body (this is how a rare cross-connection
        # read-your-writes inversion under -n auto once read back 'idling'
        # instead of 'restarting'). Pin it to the setup line that caused it.
        assert cur.rowcount == 1, (
            f"_set_status: agent {agent_id} not updated (rowcount={cur.rowcount})"
        )
    db_conn.commit()


class TestExitedEndpoint:
    """POST /api/agents/{id}/exited — an agent reports its own process exit and
    the gateway finalizes the row. This is where the guarded status flip lives
    now (it used to run inline in the agent's finally block).

    The `WHERE status IN ('running','idling')` guard is
    load-bearing: a restart goes claim -> 'restarting' -> END -> process exit
    -> this endpoint, and clobbering 'restarting' to 'terminated' would strand
    the restarter — the negative case is asserted below alongside the happy
    paths.
    """

    def test_running_to_terminated(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'running' (including an early boot-phase death) →
        'terminated', so the row never petrifies."""
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        agent_id = _spawn_agent()
        _set_status(db_conn, agent_id, "running")
        with TestClient(app) as client:
            resp = client.post(f"/api/agents/{agent_id}/exited")
        assert resp.status_code == 204
        # Read from a fresh connection — avoids cross-connection visibility lag
        # (synchronous_commit=off in test Postgres).
        with shared.db.connect() as fresh_conn, fresh_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
            row = cur.fetchone()
        assert row is not None and row[0] == "terminated"

    def test_idling_to_terminated(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'idling' (claim waiting on inbound) → 'terminated'."""
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        agent_id = _spawn_agent()
        _set_status(db_conn, agent_id, "idling")
        with TestClient(app) as client:
            resp = client.post(f"/api/agents/{agent_id}/exited")
        assert resp.status_code == 204
        # Read from a fresh connection — avoids cross-connection visibility lag.
        with shared.db.connect() as fresh_conn, fresh_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
            row = cur.fetchone()
        assert row is not None and row[0] == "terminated"

    def test_restarting_unchanged(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'restarting' is NOT touched — the restarter daemon owns that row; a
        restart's process-exit hitting /exited must leave it for the restarter."""
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        agent_id = _spawn_agent()
        _set_status(db_conn, agent_id, "restarting")
        # Read back to confirm the UPDATE is visible before the POST — a
        # cross-connection visibility lag (synchronous_commit=off in test
        # Postgres) would otherwise let the guarded UPDATE see a stale status.
        assert _status(db_conn, agent_id) == "restarting", (
            f"_set_status wrote 'restarting' but read back {_status(db_conn, agent_id)!r}"
        )
        with TestClient(app) as client:
            resp = client.post(f"/api/agents/{agent_id}/exited")
        assert resp.status_code == 204
        # Read status from a fresh connection so we see the latest committed
        # state — same pattern as the claim-test fix in #1237.
        with shared.db.connect() as fresh_conn, fresh_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
            row = cur.fetchone()
        assert row is not None and row[0] == "restarting"  # untouched

    def test_idempotent(self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
        """Calling twice is safe (the finally block may run more than once) —
        the second call is a no-op on the already-'terminated' row."""
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        agent_id = _spawn_agent()
        _set_status(db_conn, agent_id, "running")
        with TestClient(app) as client:
            assert client.post(f"/api/agents/{agent_id}/exited").status_code == 204
            assert client.post(f"/api/agents/{agent_id}/exited").status_code == 204
        # Read from a fresh connection — avoids cross-connection visibility lag.
        with shared.db.connect() as fresh_conn, fresh_conn.cursor() as cur:
            cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
            row = cur.fetchone()
        assert row is not None and row[0] == "terminated"


class TestLaunchConfirmTerminatedBoot:
    """`_wait_for_agent_claim` distinguishes a confirmed claim
    from a boot rejected before claiming. The early schema gate marks a
    behind-schema boot 'idling' -> 'terminated' with no pid; that never
    started, so confirmation must raise rather than report success. A
    'terminated' row that still carries a pid DID claim (then exited fast) and
    is a confirmed start.
    """

    @staticmethod
    def _set(db_conn: psycopg.Connection, agent_id: int, status: str, pid: int | None) -> None:
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = %s, pid = %s WHERE id = %s",
                (status, pid, agent_id),
            )
        db_conn.commit()

    def test_terminated_with_null_pid_raises_boot_rejected(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'terminated' + NULL pid = never claimed (schema gate / pre-claim
        reap) -> confirmation raises immediately, no waiting."""
        agent_id = _spawn_agent()
        self._set(db_conn, agent_id, "terminated", None)

        with pytest.raises(RuntimeError, match=r"boot rejected before claiming"):
            _wait_for_agent_claim(agent_id)

    def test_terminated_with_pid_returns(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'terminated' + a pid = the agent claimed ('running' wrote the pid)
        then exited fast; the claim ran, so confirmation succeeds."""
        agent_id = _spawn_agent()
        self._set(db_conn, agent_id, "terminated", 4242)

        _wait_for_agent_claim(agent_id)  # returns, no raise

    def test_running_returns(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordinary confirmed-start case still returns."""
        agent_id = _spawn_agent()
        self._set(db_conn, agent_id, "running", 4242)

        _wait_for_agent_claim(agent_id)  # returns, no raise


class TestSpawnerValidation:
    """create_agent_row rejects malformed spawner values that would produce
    "Agent None" in the frontend tree."""

    def test_agent_none_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            _spawn_agent(spawner="agent:None")

    def test_agent_empty_id_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            _spawn_agent(spawner="agent:")

    def test_agent_alphabetic_id_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            _spawn_agent(spawner="agent:abc")

    def test_agent_zero_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            _spawn_agent(spawner="agent:0")

    def test_agent_valid_id_accepted(self, db_conn, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        # spawner="agent:42" is valid — must not raise
        new_id = _spawn_agent(spawner="agent:42")
        row = _agents_row(db_conn, new_id)  # pyright: ignore[reportUnknownArgumentType]
        assert row is not None
        assert row[1] == "agent:42"

    def test_user_spawner_accepted(self, db_conn, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        new_id = _spawn_agent(spawner="user")
        row = _agents_row(db_conn, new_id)  # pyright: ignore[reportUnknownArgumentType]
        assert row is not None
        assert row[1] == "user"

    def test_arbitrary_spawner_accepted(self, db_conn, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        new_id = _spawn_agent(spawner="claude-code")
        row = _agents_row(db_conn, new_id)  # pyright: ignore[reportUnknownArgumentType]
        assert row is not None
        assert row[1] == "claude-code"

    def test_agent_negative_id_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ops.agent_launch._launch_agent_process", lambda *_a, **_k: None)  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            _spawn_agent(spawner="agent:-1")

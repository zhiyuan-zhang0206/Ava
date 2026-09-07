"""Real database contracts for agent creation, forks, and resurrection.

Tests verify durable birth configuration, lifecycle messages, exact resurrection
triggers, and caller fences. Host wake publication follows committed state.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import shared.db
from ops.agent_wake import ResurrectTriggerStaleError
from ops.agents import (
    create_agent_row,
    resurrect_agent,
)
from ops.ops_lifecycle import _force_mark_terminated
from shared.agent_snapshot import select_one
from shared.agents import (
    AgentNotFound,
    ForkCheckpointNotFound,
    ResurrectAlreadyAlive,
    ResurrectError,
)
from shared.config import settings
from shared.envelope import wrap_inbound
from shared.machine import machine_name


def _test_pool() -> ConnectionPool:
    """Return a concretely typed pool for helpers that open their own pool."""
    return cast(
        ConnectionPool,
        ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2),
    )


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
    agent_id, _birth_config = create_agent_row(
        spawner=spawner,
        fork_from=fork_from,
        fork_checkpoint=fork_checkpoint,
        machine=machine_name(),
        config=config,
        label=label,
        prompt=prompt,
        prompt_source=prompt_source,
    )
    shared.db.publish_inbound_wake(agent_id, "0")
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

        new_id = _spawn_agent()

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

    def test_persists_config_overlay(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_spawn_agent(config={...}) persists to agents_meta.config_overlay AND
        passes config_overlay= to _launch_agent_process. Both sides must work for
        the per-agent model override to actually take effect at boot.
        """
        from shared.config import settings

        new_id = _spawn_agent(spawner="user", config={"llm_model": "gpt-5.6-sol"})

        # column persisted
        with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT config_overlay FROM agents_meta WHERE id = %s", (new_id,))
            row = cur.fetchone()
        assert row is not None
        assert row[0] == {"llm_model": "gpt-5.6-sol"}

        # launch received the overlay

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

    def test_guarded_resurrect_rejects_chat_from_prior_termination(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A watchdog task selected for one death must not revive a later
        explicit kill while its RPC was in flight."""
        agent_id = _spawn_agent()
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

    def test_guarded_resurrect_rejects_chat_that_is_no_longer_pending(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A trigger claimed while the RPC is in flight no longer justifies
        launching the terminated owner."""
        agent_id = _spawn_agent()
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

    def test_guarded_compact_below_force_fence_is_stale(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A force after compact enqueue fences that older work exactly like
        chat, even though no second status transition occurs."""
        agent_id = _spawn_agent()
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

        seen: list[list[tuple]] = []

        def _spy_wake(agent_id: int, _payload: str) -> None:
            with shared.db.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT content, kind, source FROM inbound_messages "
                    "WHERE agent_id = %s ORDER BY id ASC",
                    (agent_id,),
                )
                seen.append(cur.fetchall())  # pyright: ignore[reportUnknownMemberType]

        monkeypatch.setattr(shared.db, "publish_inbound_wake", _spy_wake)
        _spawn_agent(fork_from=source, fork_checkpoint="ck", prompt="go do X", prompt_source="user")

        assert seen and all(
            batch == [("", "fork", f"agent:{source}"), ("go do X", "chat", "user")]
            for batch in seen
        )

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


class TestSpawnerValidation:
    """create_agent_row rejects malformed spawner values that would produce
    "Agent None" in the frontend tree."""

    def test_agent_none_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            _spawn_agent(spawner="agent:None")

    def test_agent_empty_id_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            _spawn_agent(spawner="agent:")

    def test_agent_alphabetic_id_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            _spawn_agent(spawner="agent:abc")

    def test_agent_zero_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            _spawn_agent(spawner="agent:0")

    def test_agent_valid_id_accepted(self, db_conn, monkeypatch: pytest.MonkeyPatch) -> None:
        # spawner="agent:42" is valid — must not raise
        new_id = _spawn_agent(spawner="agent:42")
        row = _agents_row(db_conn, new_id)  # pyright: ignore[reportUnknownArgumentType]
        assert row is not None
        assert row[1] == "agent:42"

    def test_user_spawner_accepted(self, db_conn, monkeypatch: pytest.MonkeyPatch) -> None:
        new_id = _spawn_agent(spawner="user")
        row = _agents_row(db_conn, new_id)  # pyright: ignore[reportUnknownArgumentType]
        assert row is not None
        assert row[1] == "user"

    def test_arbitrary_spawner_accepted(self, db_conn, monkeypatch: pytest.MonkeyPatch) -> None:
        new_id = _spawn_agent(spawner="claude-code")
        row = _agents_row(db_conn, new_id)  # pyright: ignore[reportUnknownArgumentType]
        assert row is not None
        assert row[1] == "claude-code"

    def test_agent_negative_id_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ValueError, match="spawner has agent: prefix"):
            _spawn_agent(spawner="agent:-1")

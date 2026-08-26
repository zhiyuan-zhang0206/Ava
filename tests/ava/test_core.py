"""Unit tests for `ava.terminate` / `ava.restart` behavior.

Each throws a dedicated `SystemExit` subclass (`AgentTermination` / `AgentRestart`),
not swallowed by `except Exception`; the difference is what kind is written to the inbound queue:
- terminate: writes 'terminate' → claim appends lifecycle marker + goto END process exits
- restart:   writes 'restart'   → claim UPDATE status='restarting' + goto END,
             restarter daemon automatically respawns a fresh process attached to the same agent_id + delivers
             'restart_completed' inbound so the new process wakes up knowing the restart is done

(Explicit `idle()` removed: the model not calling execute_code this turn = automatically stops the turn, no SDK call needed.)
"""

from __future__ import annotations

import psycopg
import pytest

import ava
from shared.config import set_field, settings
from tests.conftest import spawn_agent


def _inbound_rows(db: psycopg.Connection, agent_id: int) -> list[tuple]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT content, kind, source FROM inbound_messages "
            "WHERE agent_id = %s ORDER BY id ASC",
            (agent_id,),
        )
        return cur.fetchall()


class TestLifecycleExceptions:
    def test_agent_termination_not_caught_by_except_exception(
        self,
        db_conn: psycopg.Connection,
    ) -> None:
        """AgentTermination inherits SystemExit, not Exception — the broad
        except written by the agent will not accidentally swallow lifecycle signals."""

        def _raise() -> None:
            raise ava.self.AgentTermination

        caught_by_exception = False
        try:
            try:
                _raise()
            except Exception:
                caught_by_exception = True
        except ava.self.AgentTermination:
            pass
        assert not caught_by_exception

    def test_agent_restart_not_caught_by_except_exception(
        self,
        db_conn: psycopg.Connection,
    ) -> None:
        """AgentRestart inherits SystemExit, not Exception — same as AgentTermination."""

        def _raise() -> None:
            raise ava.self.AgentRestart

        caught_by_exception = False
        try:
            try:
                _raise()
            except Exception:
                caught_by_exception = True
        except ava.self.AgentRestart:
            pass
        assert not caught_by_exception


class TestTerminate:
    def test_terminate_inserts_terminate_inbound_to_self(
        self, db_conn: psycopg.Connection, monkeypatch
    ) -> None:
        """terminate self-inserts a kind='terminate' source='self' into its own agent.
        source='self' lets the claim dispatch produce the "by yourself" marker (distinguishing from external
        user / agent:N triggered "by {source}", precisely expressing "suicide" semantics)."""
        ava._boot._agent_id = spawn_agent()  # self identity
        with pytest.raises(ava.self.AgentTermination):
            ava.self.terminate()
        assert _inbound_rows(db_conn, ava.self.AGENT_ID) == [("", "terminate", "self")]


class TestRestart:
    def test_restart_inserts_restart_inbound_to_self(
        self, db_conn: psycopg.Connection, monkeypatch
    ) -> None:
        """restart self-inserts a kind='restart' source='self' into its own agent.
        The restarter daemon will handle the respawn (this test only verifies the SDK-side write is correct)."""
        ava._boot._agent_id = spawn_agent()  # self identity
        with pytest.raises(ava.self.AgentRestart):
            ava.self.restart()
        assert _inbound_rows(db_conn, ava.self.AGENT_ID) == [("", "restart", "self")]

    def test_restart_with_config_inserts_payload(
        self, db_conn: psycopg.Connection, monkeypatch
    ) -> None:
        """`ava.self.restart(config_overlay={...})` (PR-E) — payload JSONB holds config_overlay.

        Validation + INSERT path exercised, validate_config_overlay passes then payload serialized
        into inbound_messages.payload.
        """
        ava._boot._agent_id = spawn_agent()
        with pytest.raises(ava.self.AgentRestart):
            ava.self.restart(config_overlay={"auto_compact_fraction": 0.7})
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT kind, source, payload FROM inbound_messages "
                "WHERE agent_id = %s ORDER BY id DESC LIMIT 1",
                (ava.self.AGENT_ID,),
            )
            row = cur.fetchone()
        assert row is not None
        kind, source, payload = row
        assert kind == "restart"
        assert source == "self"
        assert payload == {"config_overlay": {"auto_compact_fraction": 0.7}}

    def test_restart_with_config_merges_into_overlay_column(
        self, db_conn: psycopg.Connection, monkeypatch
    ) -> None:
        """restart(config_overlay=) merges into agents_meta.config_overlay rather than
        replacing it: pre-existing keys survive, new keys are added.

        Also verifies the inbound payload still carries only the this-restart
        diff (not the merged result) so the restart_completed marker shows the
        right diff text.
        """
        ava._boot._agent_id = spawn_agent()
        # Pre-seed an existing overlay key directly in the column.
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET config_overlay = %s::jsonb WHERE id = %s",
                ('{"auto_compact_fraction": 0.7}', ava.self.AGENT_ID),
            )
        db_conn.commit()

        with pytest.raises(ava.self.AgentRestart):
            ava.self.restart(config_overlay={"llm_model": "gpt-5.6-sol"})

        # Column should have MERGED result (pre-existing key preserved).
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT config_overlay FROM agents_meta WHERE id = %s",
                (ava.self.AGENT_ID,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == {"auto_compact_fraction": 0.7, "llm_model": "gpt-5.6-sol"}

        # Inbound payload carries only the this-restart diff, not the merged result.
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT payload FROM inbound_messages "
                "WHERE agent_id = %s AND kind = 'restart' ORDER BY id DESC LIMIT 1",
                (ava.self.AGENT_ID,),
            )
            inbound_row = cur.fetchone()
        assert inbound_row is not None
        assert inbound_row[0] == {"config_overlay": {"llm_model": "gpt-5.6-sol"}}

    def test_restart_with_invalid_config_raises_and_no_inbound(
        self, db_conn: psycopg.Connection, monkeypatch
    ) -> None:
        """Non-per_agent field → InvalidConfigOverlay; does **not** deliver inbound, process does not exit."""
        ava._boot._agent_id = spawn_agent()
        with pytest.raises(ava.self.InvalidConfigOverlay, match=r"typo|per_agent"):  # type: ignore[attr-defined]
            ava.self.restart(config_overlay={"definitely_not_a_field": 1})
        # No inbound delivered (process will not exit)
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM inbound_messages WHERE agent_id = %s AND kind = 'restart'",
                (ava.self.AGENT_ID,),
            )
            count_row = cur.fetchone()
            assert count_row is not None
            assert count_row[0] == 0

    def test_restart_with_unknown_model_raises_without_persisting(
        self, db_conn: psycopg.Connection, monkeypatch
    ) -> None:
        """A model typo is rejected before either persistent restart side effect."""
        ava._boot._agent_id = spawn_agent()
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT config_overlay FROM agents_meta WHERE id = %s",
                (ava.self.AGENT_ID,),
            )
            before_row = cur.fetchone()
        assert before_row is not None
        assert before_row[0] in (None, {})

        with pytest.raises(ava.self.InvalidConfigOverlay, match="not a registered model"):  # type: ignore[attr-defined]
            ava.self.restart(config_overlay={"llm_model": "deepseek-v4-flash-vision"})

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM inbound_messages WHERE agent_id = %s AND kind = 'restart'",
                (ava.self.AGENT_ID,),
            )
            inbound_count_row = cur.fetchone()
            cur.execute(
                "SELECT config_overlay FROM agents_meta WHERE id = %s",
                (ava.self.AGENT_ID,),
            )
            after_row = cur.fetchone()
        assert inbound_count_row is not None
        assert inbound_count_row[0] == 0
        assert after_row is not None
        assert after_row[0] == before_row[0]


class TestPauseHeartbeat:
    def test_pause_heartbeat_sets_window_and_emits_event(
        self, db_conn: psycopg.Connection, monkeypatch
    ) -> None:
        """pause_heartbeat does two things (same cursor, atomic): ① UPDATE
        agents_meta.heartbeat_paused_until = now()+duration; ② INSERT a
        heartbeat_paused events row (payload.duration_s) — the inspector's Last
        Pause relies on this. Both read back via db_conn."""
        ava._boot._agent_id = spawn_agent()  # self identity
        ava.self.pause_heartbeat(1800)
        from shared import telemetry

        telemetry.sync()  # the event lands via the unified emitter's drain
        db_conn.rollback()  # fresh snapshot — the emitter wrote on its own connection
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT EXTRACT(EPOCH FROM (heartbeat_paused_until - now())) "
                "FROM agents_meta WHERE id = %s",
                (ava.self.AGENT_ID,),
            )
            window_row = cur.fetchone()
        assert window_row is not None
        assert window_row[0] == pytest.approx(1800, abs=5)  # pyright: ignore[reportUnknownMemberType]
        # The event row lives in the JSONL mirror (the PG events copy is a
        # read-only archive since the LGTM cutover, task #1197 close-C).
        import json as _json
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        from shared.paths import logs_dir

        day = _dt.now(_UTC).strftime("%Y%m%d")
        path = logs_dir() / f"events-{day}.jsonl"
        assert path.exists(), "mirror file missing"
        event_row = None
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            obj = _json.loads(line)
            if (
                obj.get("event_name") == "heartbeat_paused"
                and obj.get("agent_id") == ava.self.AGENT_ID
                and obj.get("category") == "telemetry"
            ):
                event_row = obj
                break
        assert event_row is not None
        assert float(event_row["attributes"]["duration_s"]) == 1800.0

    @pytest.mark.parametrize("bad", [0, -1, settings.agent.heartbeat_pause_max_seconds + 1])
    def test_pause_heartbeat_rejects_out_of_range(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, bad: float
    ) -> None:
        """duration must be in (0, configured limit] — invalid calls do not write or emit."""
        ava._boot._agent_id = spawn_agent()
        from shared import telemetry

        def _unexpected_emit(*_args: object, **_kwargs: object) -> None:
            pytest.fail("invalid duration emitted telemetry")

        monkeypatch.setattr(
            telemetry,
            "emit",
            _unexpected_emit,
        )
        with pytest.raises(ValueError, match=r"greater than 0|at most"):
            ava.self.pause_heartbeat(bad)
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT heartbeat_paused_until FROM agents_meta WHERE id = %s",
                (ava.self.AGENT_ID,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] is None

    def test_pause_heartbeat_lower_cluster_default(self, db_conn: psycopg.Connection) -> None:
        """The configured cluster limit accepts its inclusive upper boundary."""
        original_limit = settings.agent.heartbeat_pause_max_seconds
        set_field("heartbeat_pause_max_seconds", 3600.0)
        try:
            ava._boot._agent_id = spawn_agent()
            ava.self.pause_heartbeat(3600)
            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT EXTRACT(EPOCH FROM (heartbeat_paused_until - now())) "
                    "FROM agents_meta WHERE id = %s",
                    (ava.self.AGENT_ID,),
                )
                window_row = cur.fetchone()
            assert window_row is not None
            assert window_row[0] == pytest.approx(3600, abs=5)  # pyright: ignore[reportUnknownMemberType]
            with pytest.raises(ValueError, match=r"at most"):
                ava.self.pause_heartbeat(3601)
        finally:
            set_field("heartbeat_pause_max_seconds", original_limit)

    def test_pause_heartbeat_per_agent_override_wins(self) -> None:
        """The effective per-agent overlay limit is read when the SDK is called."""
        original_limit = settings.agent.heartbeat_pause_max_seconds
        set_field("heartbeat_pause_max_seconds", 172800.0)
        try:
            ava._boot._agent_id = spawn_agent()
            ava.self.pause_heartbeat(172800)
        finally:
            set_field("heartbeat_pause_max_seconds", original_limit)
        with pytest.raises(ValueError, match=r"at most"):
            ava.self.pause_heartbeat(172800)


class TestRestartCompletedMarker:
    """`_render_restart_completed_marker` payload rendering — PR-E adds `with config {…}` suffix."""

    def test_marker_no_payload(self) -> None:
        from agent.graph._claim import _render_restart_completed_marker

        msg = _render_restart_completed_marker("self", None)
        assert "restarted by yourself" in msg
        assert "with config" not in msg

    def test_marker_with_overlay(self) -> None:
        from agent.graph._claim import _render_restart_completed_marker

        msg = _render_restart_completed_marker(
            "self", {"config_overlay": {"auto_compact_fraction": 0.7}}
        )
        assert "restarted by yourself" in msg
        assert "with config {auto_compact_fraction=0.7}" in msg

    def test_marker_system_update_with_overlay(self) -> None:
        """system:update source + overlay simultaneously — marker has updated wording + overlay diff."""
        from agent.graph._claim import _render_restart_completed_marker

        msg = _render_restart_completed_marker(
            "system:update",
            {"config_overlay": {"auto_compact_fraction": 0.5}},
        )
        assert "updated and restarted" in msg
        assert "with config {auto_compact_fraction=0.5}" in msg

    def test_marker_redacts_credential_like_overlay_keys(self) -> None:
        """Credential-like overlay keys render as <redacted> — the marker lands in
        the checkpoint / timeline / LLM context, so it must not carry a second
        plaintext copy of a value like an api_key (2026-08-08 audit, P1-3)."""
        from agent.graph._claim import _render_restart_completed_marker

        msg = _render_restart_completed_marker(
            "user",
            {
                "config_overlay": {
                    "deepseek_api_key": "sk-secret-value",
                    "auto_compact_fraction": 0.7,
                    "cluster_secret": "hunter2",
                }
            },
        )
        assert "deepseek_api_key=<redacted>" in msg
        assert "cluster_secret=<redacted>" in msg
        assert "sk-secret-value" not in msg
        assert "hunter2" not in msg
        # non-sensitive keys still render their value
        assert "auto_compact_fraction=0.7" in msg


# peer terminate / restart withdrawn (2026-05-04) — agents can only send_message to
# communicate with peers, letting the peer decide idle/terminate/restart itself (autonomous peer model).
# Directly controlling a live agent is both unsafe (may be doing important work) and breaks the peer model.
# user-facing gateway endpoint (POST /api/agents/{id}/terminate|restart) is kept.


class TestAvaDBAutocommit:
    def test_ava_db_recovers_after_sql_error(
        self,
        db_conn: psycopg.Connection,
    ) -> None:
        """After an SQL error, ava.DB can still be used immediately — autocommit=True prevents the
        connection from getting stuck in InFailedSqlTransaction state (otherwise one test throwing an
        SQL error without rollback would cause subsequent tests using ava.DB to all cascade-fail,
        as encountered in the smoke flow).
        """
        # Deliberately trigger an SQL error
        with pytest.raises(psycopg.errors.UndefinedTable), ava.DB.cursor() as cur:
            cur.execute("SELECT * FROM nonexistent_table_xyz")

        # Immediately use ava.DB after the error — under autocommit the conn is back to IDLE, should not error again
        with ava.DB.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)

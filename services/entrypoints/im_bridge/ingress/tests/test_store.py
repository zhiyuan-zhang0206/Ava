"""Real database proof of source retention, atomic chat and account/cursor fencing."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg
import pytest

from base.db import Database, create_agent
from services.entrypoints.im_bridge.ingress.store import WeixinIngressStore
from services.entrypoints.im_bridge.ingress.types import (
    IngressIdentityConflictError,
    IngressRoute,
    IngressRouteKind,
    IngressStatus,
    ProviderSource,
    StalePollBindingError,
)


def source(message_id: str = "1", *, account: str = "account") -> ProviderSource:
    return ProviderSource(
        namespace="https://provider.example",
        account_id=account,
        sender_id="human",
        message_id=message_id,
    )


def test_atomic_chat_receipt_replay_survives_deletion_and_selection_change(
    db_conn: psycopg.Connection,
    database: Database,
) -> None:
    first, later = create_agent(db_conn), create_agent(db_conn)
    with database.pool(min_size=1, max_size=2) as pool:
        store = WeixinIngressStore(pool)
        binding = store.initialize("https://provider.example", "account", None)
        binding = store.begin_cutover(binding, expected_cursor="")
        binding = store.checkpoint(binding, "", "", provider_empty=True)
        accepted = store.retain(
            binding,
            source(),
            {"text": "original"},
            {"context_token": "private"},
            IngressRoute(kind=IngressRouteKind.CHAT, text="original", agent_id=first),
            "stable-source",
        )
        assert accepted.status == IngressStatus.ACCEPTED and accepted.inbound_id is not None
        db_conn.execute("DELETE FROM inbound_messages WHERE id=%s", (accepted.inbound_id,))
        db_conn.commit()
        replay = store.retain(
            binding,
            source(),
            {"text": "original"},
            {"context_token": "rotated"},
            IngressRoute(kind=IngressRouteKind.CHAT, text="original", agent_id=later),
            "stable-source",
        )
        assert replay == accepted
        assert db_conn.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id IN (%s,%s)", (first, later)
        ).fetchone() == (0,)
        with pytest.raises(IngressIdentityConflictError):
            store.retain(
                binding,
                source(),
                {"text": "changed"},
                {},
                IngressRoute(kind=IngressRouteKind.CHAT, text="changed", agent_id=first),
                "stable-source",
            )


def test_fresh_concurrent_sources_commit_only_one_inbound(
    db_conn: psycopg.Connection,
    database: Database,
) -> None:
    agent = create_agent(db_conn)
    barrier = Barrier(2)
    with database.pool(min_size=2, max_size=2) as pool:
        store = WeixinIngressStore(pool)
        binding = store.initialize("https://provider.example", "account", None)
        binding = store.begin_cutover(binding, expected_cursor="")
        binding = store.checkpoint(binding, "", "", provider_empty=True)

        def accept(_index: int) -> int:
            barrier.wait(timeout=5)
            return store.retain(
                binding,
                source(),
                {"text": "hello"},
                {},
                IngressRoute(kind=IngressRouteKind.CHAT, text="hello", agent_id=agent),
                "same-key",
            ).id

        with ThreadPoolExecutor(max_workers=2) as executor:
            ids = list(executor.map(accept, range(2)))
        assert ids[0] == ids[1]
        assert db_conn.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
        ).fetchone() == (1,)


def test_command_crash_is_retained_uncertain_and_cursor_can_continue(
    database: Database,
) -> None:
    with database.pool(min_size=1, max_size=2) as pool:
        store = WeixinIngressStore(pool)
        binding = store.initialize("https://provider.example", "account", None)
        binding = store.begin_cutover(binding, expected_cursor="")
        binding = store.checkpoint(binding, "", "", provider_empty=True)
        retained = store.retain(
            binding,
            source(),
            {"text": "/spawn"},
            {},
            IngressRoute(kind=IngressRouteKind.COMMAND, text="/spawn"),
            "source-key",
        )
        with pytest.raises(RuntimeError, match="unprocessed"):
            store.checkpoint(binding, "", "opaque-next")
        claimed = store.claim(binding, retained.id)
        assert claimed is not None and claimed.attempt_id is not None
        restarted = store.initialize("https://provider.example", "account", None)
        store.recover_claims(restarted)
        replay = store.lookup(restarted, source(), {"text": "/spawn"})
        assert replay is not None and replay.status == IngressStatus.UNCERTAIN
        assert replay.attempt_id == claimed.attempt_id
        assert store.claim(restarted, retained.id) is None
        with pytest.raises(StalePollBindingError):
            store.finish(binding, claimed, IngressStatus.ACCEPTED, None, {"selected_agent_id": 1})
        advanced = store.checkpoint(restarted, "", "opaque-next")
        assert advanced.cursor == "opaque-next"
        with pytest.raises(StalePollBindingError):
            store.checkpoint(restarted, "", "wrong-cursor")


def test_absent_json_is_not_history_proof_and_operator_binding_only_drains(
    db_conn: psycopg.Connection,
    database: Database,
) -> None:
    agent = create_agent(db_conn)
    with database.pool(min_size=1, max_size=2) as pool:
        store = WeixinIngressStore(pool)
        held = store.initialize("https://provider.example", "account", None)
        assert held.held
        with pytest.raises(RuntimeError, match="operator account binding"):
            store.lookup(held, source(), {"text": "unknown"})
        draining = store.begin_cutover(held, expected_cursor="")
        with pytest.raises(RuntimeError, match="active cutover"):
            store.retain(
                draining,
                source(),
                {"text": "unknown"},
                {},
                IngressRoute(kind=IngressRouteKind.CHAT, text="unknown", agent_id=agent),
                "new-key",
            )
        for message_id, text in [("1", "spawn:go"), ("2", "/switch 4"), ("3", "ordinary")]:
            quarantined = store.quarantine_or_adopt(
                draining,
                source(message_id),
                {"text": text},
                {"message_id": message_id},
                text,
                "key" + message_id,
            )
            assert quarantined is not None
            assert quarantined.status == IngressStatus.QUARANTINED
            assert quarantined.attempt_id is None and quarantined.inbound_id is None
            assert store.claim(draining, quarantined.id) is None
        later = store.checkpoint(draining, "", "opaque-intermediate")
        assert not later.held and later.state.value == "draining"
        final = store.checkpoint(later, "opaque-intermediate", "opaque-empty", provider_empty=True)
        assert final.state.value == "active"
        assert db_conn.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
        ).fetchone() == (0,)
        assert db_conn.execute(
            "SELECT count(*) FROM weixin_ingress_receipts WHERE status='quarantined'"
        ).fetchone() == (3,)


def test_legacy_account_binding_preserves_opaque_cursor_and_fences_previous_owner(
    database: Database,
) -> None:
    with database.pool(min_size=1, max_size=2) as pool:
        store = WeixinIngressStore(pool)
        held = store.initialize("https://provider.example", "account", "opaque-old")
        assert held.held and held.cursor == "opaque-old"
        with pytest.raises(ValueError, match="exact held cursor"):
            store.begin_cutover(held, expected_cursor="")
        replacement = store.initialize("https://provider.example", "other-account", None)
        with pytest.raises(StalePollBindingError):
            store.begin_cutover(held, expected_cursor="opaque-old")
        assert replacement.held
        resumed = store.initialize("https://provider.example", "account", None)
        assert resumed.held and resumed.cursor == "opaque-old"
        draining = store.begin_cutover(resumed, expected_cursor="opaque-old")
        assert draining.cursor == "opaque-old"


def test_post_insert_failure_rolls_back_provider_receipt_chat_and_central_audit(
    db_conn: psycopg.Connection,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.entrypoints.im_bridge.ingress import store as store_owner

    agent = create_agent(db_conn)
    with database.pool(min_size=1, max_size=2) as pool:
        store = WeixinIngressStore(pool)
        binding = store.initialize("https://provider.example", "account", None)
        binding = store.begin_cutover(binding, expected_cursor="")
        binding = store.checkpoint(binding, "", "", provider_empty=True)

        def fail_after_real_insert(_row: tuple[object, ...]) -> None:
            raise RuntimeError("receipt result failed after native insert")

        monkeypatch.setattr(store_owner, "receipt_from_row", fail_after_real_insert)
        with pytest.raises(RuntimeError, match="after native insert"):
            store.retain(
                binding,
                source(),
                {"text": "original"},
                {},
                IngressRoute(kind=IngressRouteKind.CHAT, text="original", agent_id=agent),
                "source-once",
            )
    assert db_conn.execute("SELECT count(*) FROM weixin_ingress_receipts").fetchone() == (0,)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (0,)
    assert db_conn.execute(
        "SELECT count(*) FROM audit_events WHERE agent_id=%s AND event_name='send_message'",
        (agent,),
    ).fetchone() == (0,)


def test_failed_empty_checkpoint_rolls_back_cursor_and_activation_together(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import Generator
    from contextlib import contextmanager

    from psycopg_pool import ConnectionPool

    from services.entrypoints.im_bridge.ingress import store as store_owner

    original_transaction = store_owner.write_transaction
    with database.pool(min_size=1, max_size=2) as pool:
        store = WeixinIngressStore(pool)
        held = store.initialize("https://provider.example", "account", "legacy-opaque")
        draining = store.begin_cutover(held, expected_cursor="legacy-opaque")

        @contextmanager
        def fail_before_commit(
            native_pool: ConnectionPool,
        ) -> Generator[psycopg.Connection, None, None]:
            with original_transaction(native_pool) as conn:
                yield conn
                # The real checkpoint SQL already ran, but its commit never succeeds.
                assert conn.execute(
                    "SELECT state,cursor FROM weixin_ingress_cursors"
                ).fetchone() == ("active", "new-empty-opaque")
                raise RuntimeError("injected checkpoint commit interruption")

        monkeypatch.setattr(store_owner, "write_transaction", fail_before_commit)
        with pytest.raises(RuntimeError, match="checkpoint commit interruption"):
            store.checkpoint(draining, "legacy-opaque", "new-empty-opaque", provider_empty=True)
        monkeypatch.setattr(store_owner, "write_transaction", original_transaction)
        restored = store.refresh_binding(draining)
        assert restored.cursor == "legacy-opaque" and restored.state.value == "draining"
        committed = store.checkpoint(
            restored, "legacy-opaque", "new-empty-opaque", provider_empty=True
        )
        assert committed.cursor == "new-empty-opaque" and committed.state.value == "active"

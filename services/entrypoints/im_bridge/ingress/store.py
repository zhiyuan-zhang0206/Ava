"""Account-fenced Weixin source retention and native chat admission."""

import json
from typing import Any
from uuid import uuid4

from psycopg import Connection, sql
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base import telemetry
from base.agents.messages.chat_delivery import insert_chat_inbound_in_transaction
from base.db.transaction import write_transaction
from base.deploy.maintenance import admission
from services.entrypoints.im_bridge.ingress.types import (
    IngressBindingState,
    IngressIdentityConflictError,
    IngressReceipt,
    IngressRoute,
    IngressRouteKind,
    IngressStatus,
    PollBinding,
    ProviderSource,
    StalePollBindingError,
)

_RECEIPT_COLUMNS = "id,namespace,account_id,sender_id,message_id,route,status,attempt_id,inbound_id,outcome_reason,result"


def receipt_from_row(row: tuple[Any, ...]) -> IngressReceipt:
    return IngressReceipt.model_validate_json(
        json.dumps(
            {
                "id": row[0],
                "source": dict(
                    zip(
                        ("namespace", "account_id", "sender_id", "message_id"),
                        row[1:5],
                        strict=True,
                    )
                ),
                "route": row[5],
                "status": row[6],
                "attempt_id": str(row[7]) if row[7] is not None else None,
                "inbound_id": row[8],
                "outcome_reason": row[9],
                "result": row[10],
            }
        )
    )


class WeixinIngressStore:
    def __init__(self, pool: ConnectionPool | None) -> None:
        self.pool = pool

    def require_pool(self) -> ConnectionPool:
        if self.pool is None:
            raise RuntimeError("Weixin durable ingress requires the native database pool")
        if admission.quiesced():
            raise RuntimeError("Weixin ingress is held during maintenance")
        return self.pool

    @staticmethod
    def lock_binding(conn: Connection, binding: PollBinding) -> None:
        current = conn.execute(
            "SELECT b.namespace,b.account_id,b.epoch,c.state FROM weixin_ingress_bindings b "
            "JOIN weixin_ingress_cursors c USING(namespace,account_id) WHERE b.id=1 FOR UPDATE OF b,c"
        ).fetchone()
        if current is None or current[:3] != (binding.namespace, binding.account_id, binding.epoch):
            raise StalePollBindingError("Weixin poll account generation is superseded")
        if current[3] == IngressBindingState.HELD:
            raise RuntimeError("legacy Weixin cursor requires explicit operator account binding")

    def initialize(self, namespace: str, account_id: str, legacy_cursor: str | None) -> PollBinding:
        if not namespace or not account_id:
            raise ValueError("Weixin poll requires a qualified namespace and account")
        epoch = uuid4()
        with write_transaction(self.require_pool()) as conn:
            conn.execute(
                "INSERT INTO weixin_ingress_bindings(id,namespace,account_id,epoch) VALUES (1,%s,%s,%s) "
                "ON CONFLICT(id) DO UPDATE SET namespace=excluded.namespace,account_id=excluded.account_id,epoch=excluded.epoch",
                (namespace, account_id, epoch),
            )
            conn.execute(
                "INSERT INTO weixin_ingress_cursors(namespace,account_id,cursor,state,binding_reason) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    namespace,
                    account_id,
                    legacy_cursor or "",
                    IngressBindingState.HELD.value,
                    "legacy_unbound" if legacy_cursor is not None else "unverified_history",
                ),
            )
            row = conn.execute(
                "SELECT cursor,state FROM weixin_ingress_cursors WHERE namespace=%s AND account_id=%s",
                (namespace, account_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("Weixin ingress INSERT did not return its durable row")
            return PollBinding(
                namespace=namespace, account_id=account_id, epoch=epoch, cursor=row[0], state=row[1]
            )

    def begin_cutover(self, binding: PollBinding, *, expected_cursor: str) -> PollBinding:
        """Operator account evidence permits quarantine-only draining, never business execution."""
        with write_transaction(self.require_pool()) as conn:
            current = conn.execute(
                "SELECT namespace,account_id,epoch FROM weixin_ingress_bindings WHERE id=1 FOR UPDATE"
            ).fetchone()
            if current != (binding.namespace, binding.account_id, binding.epoch):
                raise StalePollBindingError("operator binding refers to a superseded account")
            row = conn.execute(
                "UPDATE weixin_ingress_cursors SET state='draining',binding_reason='operator_draining',updated_at=now() "
                "WHERE namespace=%s AND account_id=%s AND state='held' AND cursor=%s RETURNING cursor",
                (binding.namespace, binding.account_id, expected_cursor),
            ).fetchone()
            if row is None:
                raise ValueError("legacy cursor binding requires the exact held cursor")
        return binding.model_copy(update={"cursor": row[0], "state": IngressBindingState.DRAINING})

    @staticmethod
    def check_source(binding: PollBinding, source: ProviderSource) -> None:
        if (binding.namespace, binding.account_id) != (source.namespace, source.account_id):
            raise ValueError("Weixin source belongs to another poll account binding")

    def lookup(
        self, binding: PollBinding, source: ProviderSource, request: dict[str, Any]
    ) -> IngressReceipt | None:
        self.check_source(binding, source)
        with write_transaction(self.require_pool()) as conn:
            self.lock_binding(conn, binding)
            return self.lookup_in_transaction(conn, source, request)

    @staticmethod
    def lookup_in_transaction(
        conn: Connection, source: ProviderSource, request: dict[str, Any]
    ) -> IngressReceipt | None:
        row = conn.execute(
            sql.SQL(
                "SELECT {},request FROM weixin_ingress_receipts "
                "WHERE namespace=%s AND account_id=%s AND sender_id=%s AND message_id=%s FOR UPDATE"
            ).format(sql.SQL(_RECEIPT_COLUMNS)),
            (source.namespace, source.account_id, source.sender_id, source.message_id),
        ).fetchone()
        if row is None:
            return None
        if row[-1] != request:
            raise IngressIdentityConflictError(
                "Weixin provider source identifies a different immutable message"
            )
        return receipt_from_row(row[:-1])

    def retain(
        self,
        binding: PollBinding,
        source: ProviderSource,
        request: dict[str, Any],
        provider_payload: dict[str, Any],
        route: IngressRoute,
        client_message_id: str,
    ) -> IngressReceipt:
        self.check_source(binding, source)
        event: telemetry.Event | None = None
        with write_transaction(self.require_pool()) as conn:
            self.lock_binding(conn, binding)
            previous = self.lookup_in_transaction(conn, source, request)
            if previous is not None:
                return previous
            current_state = conn.execute(
                "SELECT state FROM weixin_ingress_cursors WHERE namespace=%s AND account_id=%s",
                (binding.namespace, binding.account_id),
            ).fetchone()
            if current_state is None or current_state[0] != IngressBindingState.ACTIVE:
                raise RuntimeError("Weixin business admission requires active cutover proof")
            status, inbound_id = IngressStatus.RETAINED, None
            reason = route.reason
            if route.kind == IngressRouteKind.CHAT:
                if route.agent_id is None:
                    status, reason = IngressStatus.REJECTED, "no_selected_agent"
                else:
                    chat, event = insert_chat_inbound_in_transaction(
                        conn,
                        agent_id=route.agent_id,
                        content=route.text,
                        source="user",
                        payload=None,
                        client_message_id=client_message_id,
                    )
                    status, inbound_id = IngressStatus.ACCEPTED, chat.inbound_id
            elif route.kind == IngressRouteKind.TERMINAL:
                status = (
                    IngressStatus.QUARANTINED
                    if route.reason == "notice_reply_mode_account_unproven"
                    else IngressStatus.REJECTED
                )
            row = conn.execute(
                sql.SQL(
                    "INSERT INTO weixin_ingress_receipts(namespace,account_id,sender_id,message_id,request,provider_payload,route,status,inbound_id,outcome_reason,completed_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s THEN now() END) RETURNING {}"
                ).format(sql.SQL(_RECEIPT_COLUMNS)),
                (
                    source.namespace,
                    source.account_id,
                    source.sender_id,
                    source.message_id,
                    Jsonb(request),
                    Jsonb(provider_payload),
                    Jsonb(route.model_dump(mode="json")),
                    status.value,
                    inbound_id,
                    reason,
                    status
                    in (IngressStatus.ACCEPTED, IngressStatus.REJECTED, IngressStatus.QUARANTINED),
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("Weixin ingress INSERT did not return its durable row")
            receipt = receipt_from_row(row)
        if event is not None:
            telemetry.emit_prepared(event)
        # The existing pending-chat watchdog owns recovery if post-commit wake is absent.
        return receipt

    def get_receipt(self, binding: PollBinding, receipt_id: int) -> IngressReceipt:
        with write_transaction(self.require_pool()) as conn:
            self.lock_binding(conn, binding)
            row = conn.execute(
                sql.SQL(
                    "SELECT {} FROM weixin_ingress_receipts WHERE id=%s AND namespace=%s AND account_id=%s"
                ).format(sql.SQL(_RECEIPT_COLUMNS)),
                (receipt_id, binding.namespace, binding.account_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("retained Weixin source receipt disappeared")
            return receipt_from_row(row)

    def refresh_binding(self, binding: PollBinding) -> PollBinding:
        with write_transaction(self.require_pool()) as conn:
            self.lock_binding(conn, binding)
            row = conn.execute(
                "SELECT cursor,state FROM weixin_ingress_cursors WHERE namespace=%s AND account_id=%s",
                (binding.namespace, binding.account_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("Weixin durable cursor disappeared")
            return binding.model_copy(
                update={"cursor": row[0], "state": IngressBindingState(row[1])}
            )

    def quarantine_or_adopt(
        self,
        binding: PollBinding,
        source: ProviderSource,
        request: dict[str, Any],
        provider_payload: dict[str, Any],
        text: str,
        client_message_id: str,
        *,
        quarantine_unknown: bool = True,
    ) -> IngressReceipt | None:
        """Adopt retained legacy chat proof; quarantine unknown history only during draining."""
        self.check_source(binding, source)
        with write_transaction(self.require_pool()) as conn:
            self.lock_binding(conn, binding)
            current = conn.execute(
                "SELECT state FROM weixin_ingress_cursors WHERE namespace=%s AND account_id=%s",
                (binding.namespace, binding.account_id),
            ).fetchone()
            expected = "draining" if quarantine_unknown else "active"
            if current is None or current[0] != expected:
                raise StalePollBindingError("Weixin adoption state changed")
            previous = self.lookup_in_transaction(conn, source, request)
            if previous is not None:
                return previous
            old = conn.execute(
                "SELECT id,agent_id,content,kind,source,payload FROM inbound_messages WHERE client_message_id=%s",
                (client_message_id,),
            ).fetchone()
            if old is None and not quarantine_unknown:
                return None
            if old is not None and (old[2], old[3], old[4], old[5]) != (text, "chat", "user", None):
                raise IngressIdentityConflictError(
                    "legacy Weixin raw identity identifies another message"
                )
            if old is not None:
                route = IngressRoute(kind=IngressRouteKind.CHAT, text=old[2], agent_id=old[1])
                status, inbound_id, reason = (
                    IngressStatus.ACCEPTED,
                    old[0],
                    "legacy_chat_receipt_adopted",
                )
            else:
                route = IngressRoute(
                    kind=IngressRouteKind.TERMINAL,
                    text=text,
                    reason="legacy_business_history_unproven",
                )
                status, inbound_id, reason = (
                    IngressStatus.QUARANTINED,
                    None,
                    "legacy_business_history_unproven",
                )
            row = conn.execute(
                sql.SQL(
                    "INSERT INTO weixin_ingress_receipts(namespace,account_id,sender_id,message_id,request,provider_payload,route,status,inbound_id,outcome_reason,completed_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) RETURNING {}"
                ).format(sql.SQL(_RECEIPT_COLUMNS)),
                (
                    source.namespace,
                    source.account_id,
                    source.sender_id,
                    source.message_id,
                    Jsonb(request),
                    Jsonb(provider_payload),
                    Jsonb(route.model_dump(mode="json")),
                    status.value,
                    inbound_id,
                    reason,
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("Weixin quarantine did not return its durable source receipt")
            return receipt_from_row(row)

    def claim(self, binding: PollBinding, receipt_id: int) -> IngressReceipt | None:
        with write_transaction(self.require_pool()) as conn:
            self.lock_binding(conn, binding)
            row = conn.execute(
                sql.SQL(
                    "UPDATE weixin_ingress_receipts SET status='claimed',attempt_id=%s "
                    "WHERE id=%s AND namespace=%s AND account_id=%s AND status='retained' AND EXISTS (SELECT 1 FROM weixin_ingress_cursors c WHERE c.namespace=weixin_ingress_receipts.namespace AND c.account_id=weixin_ingress_receipts.account_id AND c.state='active') RETURNING {}"
                ).format(sql.SQL(_RECEIPT_COLUMNS)),
                (uuid4(), receipt_id, binding.namespace, binding.account_id),
            ).fetchone()
            return receipt_from_row(row) if row is not None else None

    def guard_claim_in_transaction(
        self, conn: Connection, *, binding: PollBinding, receipt: IngressReceipt
    ) -> None:
        """Selection mutation shares the exact source epoch/attempt lock in its own native TX."""
        self.lock_binding(conn, binding)
        row = conn.execute(
            "SELECT 1 FROM weixin_ingress_receipts WHERE id=%s AND namespace=%s AND account_id=%s AND attempt_id=%s AND status='claimed' FOR UPDATE",
            (receipt.id, binding.namespace, binding.account_id, receipt.attempt_id),
        ).fetchone()
        if row is None:
            raise StalePollBindingError("Weixin command source attempt is superseded")

    def finish(
        self,
        binding: PollBinding,
        receipt: IngressReceipt,
        status: IngressStatus,
        reason: str | None,
        result: dict[str, object] | None = None,
    ) -> IngressReceipt | None:
        if status not in (IngressStatus.ACCEPTED, IngressStatus.REJECTED, IngressStatus.UNCERTAIN):
            raise ValueError("Weixin command completion requires an explicit business verdict")
        if receipt.attempt_id is None:
            raise ValueError("Weixin command completion requires its original attempt")
        if status == IngressStatus.ACCEPTED and result is None:
            raise ValueError("accepted Weixin commands require their actual owner result")
        if status == IngressStatus.UNCERTAIN and reason is None:
            raise ValueError("uncertain Weixin commands require a safe diagnostic reason")
        with write_transaction(self.require_pool()) as conn:
            self.lock_binding(conn, binding)
            row = conn.execute(
                sql.SQL(
                    "UPDATE weixin_ingress_receipts SET status=%s,outcome_reason=%s,result=%s,completed_at=now() "
                    "WHERE id=%s AND namespace=%s AND account_id=%s AND attempt_id=%s AND status='claimed' RETURNING {}"
                ).format(sql.SQL(_RECEIPT_COLUMNS)),
                (
                    status.value,
                    reason,
                    Jsonb(result) if result is not None else None,
                    receipt.id,
                    binding.namespace,
                    binding.account_id,
                    receipt.attempt_id,
                ),
            ).fetchone()
            return receipt_from_row(row) if row is not None else None

    def recover_claims(self, binding: PollBinding) -> None:
        """Startup ownership replacement retains unknown calls without ever re-executing them."""
        with write_transaction(self.require_pool()) as conn:
            self.lock_binding(conn, binding)
            conn.execute(
                "UPDATE weixin_ingress_receipts SET status='uncertain',outcome_reason='command_attempt_unresolved',completed_at=now() "
                "WHERE namespace=%s AND account_id=%s AND status='claimed'",
                (binding.namespace, binding.account_id),
            )

    def pending(self, binding: PollBinding) -> list[IngressReceipt]:
        with write_transaction(self.require_pool()) as conn:
            self.lock_binding(conn, binding)
            return [
                receipt_from_row(row)
                for row in conn.execute(
                    sql.SQL(
                        "SELECT {} FROM weixin_ingress_receipts "
                        "WHERE namespace=%s AND account_id=%s AND status='retained' ORDER BY id LIMIT 100"
                    ).format(sql.SQL(_RECEIPT_COLUMNS)),
                    (binding.namespace, binding.account_id),
                ).fetchall()
            ]

    def checkpoint(
        self,
        binding: PollBinding,
        expected_cursor: str,
        cursor: str,
        *,
        provider_empty: bool = False,
    ) -> PollBinding:
        with write_transaction(self.require_pool()) as conn:
            self.lock_binding(conn, binding)
            unfinished = conn.execute(
                "SELECT 1 FROM weixin_ingress_receipts WHERE namespace=%s AND account_id=%s "
                "AND status IN ('retained','claimed') LIMIT 1",
                (binding.namespace, binding.account_id),
            ).fetchone()
            if unfinished is not None:
                raise RuntimeError("Weixin checkpoint cannot abandon unprocessed retained sources")
            row = conn.execute(
                "UPDATE weixin_ingress_cursors SET cursor=%s,updated_at=now(), "
                "state=CASE WHEN state='draining' AND %s THEN 'active' ELSE state END, "
                "binding_reason=CASE WHEN state='draining' AND %s THEN 'operator_activated' ELSE binding_reason END "
                "WHERE namespace=%s AND account_id=%s AND cursor=%s AND state<>'held' RETURNING cursor,state",
                (
                    cursor,
                    provider_empty,
                    provider_empty,
                    binding.namespace,
                    binding.account_id,
                    expected_cursor,
                ),
            ).fetchone()
            if row is None:
                raise StalePollBindingError("Weixin opaque cursor compare-and-set rejected")
        return binding.model_copy(update={"cursor": cursor, "state": IngressBindingState(row[1])})

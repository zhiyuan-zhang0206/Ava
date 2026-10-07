"""Native alert group acceptance and recovery, using the single shared IM Outbox."""

import asyncio
import json
from enum import StrEnum
from typing import Any, Literal

from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from base.db.transaction import write_transaction
from base.deploy.maintenance import admission
from base.log import logger
from base.telemetry.alerts.native import AlertGroupOrigin, NativeAlertSourceError, load_native_group
from services.entrypoints.im_bridge.outbound_store import IMOutboxStore
from services.entrypoints.im_bridge.outbound_types import (
    OutboundIdentityConflictError,
    OutboundIntent,
    OutboundSource,
    OutboundSourceKind,
    PreparedOutboundSend,
)
from services.entrypoints.im_bridge.types import IMAdapter, SendNotStartedError


class AlertOwnerDecision(StrEnum):
    AVAILABLE = "available"
    ACCOUNT_UNAVAILABLE = "account_unavailable"
    OWNER_UNAVAILABLE = "owner_unavailable"
    UNSUPPORTED = "unsupported"
    PREPARATION_UNAVAILABLE = "preparation_unavailable"


class AlertRecipientDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: str
    account_id: str | None
    decision: AlertOwnerDecision
    recipient: str | None = None
    prepared: PreparedOutboundSend | None = None


class AlertAcceptance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    group_id: int
    intent_ids: tuple[int, ...]
    decisions: tuple[AlertRecipientDecision, ...]


class AlertAcceptanceHeldError(RuntimeError):
    """Nothing was accepted; future preparation may qualify the same native fact."""


class AlertOutboundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: int = Field(gt=0, le=2**63 - 1, strict=True)
    source_origin: Literal[AlertGroupOrigin.NATIVE]


class AlertOutboundStore:
    def __init__(self, outbound: IMOutboxStore) -> None:
        self.outbound = outbound

    @staticmethod
    def _receipt(conn: Connection, group_id: int) -> tuple[dict[str, Any], AlertAcceptance] | None:
        row = conn.execute(
            "SELECT request,decisions,intent_ids FROM im_bridge_alert_acceptances WHERE group_id=%s",
            (group_id,),
        ).fetchone()
        if row is None:
            return None
        return row[0], AlertAcceptance(group_id=group_id, decisions=row[1], intent_ids=row[2])

    def lookup(self, group_id: int) -> tuple[dict[str, Any], AlertAcceptance | None]:
        with self.outbound._pool().connection() as conn:
            previous = self._receipt(conn, group_id)
            if previous is not None:
                return previous
            return load_native_group(conn, group_id), None

    def accept(
        self, group: dict[str, Any], decisions: tuple[AlertRecipientDecision, ...]
    ) -> AlertAcceptance:
        with write_transaction(self.outbound._pool()) as conn:
            previous = self._receipt(conn, group["id"])
            if previous is not None:
                return previous[1]
            current = load_native_group(conn, group["id"], lock=True)
            previous = self._receipt(conn, group["id"])
            if previous is not None:
                return previous[1]
            if current != group:
                raise OutboundIdentityConflictError("native alert source changed before acceptance")
            intents = self._intents(current, decisions)
            if not intents:
                raise AlertAcceptanceHeldError("no native alert owner is available")
            ids = tuple(self.outbound.insert_intent(conn, intent) for intent in intents)
            frozen = [decision.model_dump(mode="json") for decision in decisions]
            conn.execute(
                "INSERT INTO im_bridge_alert_acceptances(group_id,request,decisions,intent_ids) "
                "VALUES (%s,%s,%s,%s)",
                (current["id"], Jsonb(current), Jsonb(frozen), list(ids)),
            )
            return AlertAcceptance(group_id=current["id"], decisions=decisions, intent_ids=ids)

    @staticmethod
    def _intents(
        group: dict[str, Any], decisions: tuple[AlertRecipientDecision, ...]
    ) -> tuple[OutboundIntent, ...]:
        if len({decision.channel for decision in decisions}) != len(decisions):
            raise ValueError("alert fanout has duplicate channels")
        intents: list[OutboundIntent] = []
        for decision in decisions:
            if decision.decision != AlertOwnerDecision.AVAILABLE:
                if decision.recipient is not None or decision.prepared is not None:
                    raise ValueError("unavailable channels cannot carry a fabricated intent")
                continue
            if not decision.recipient or decision.prepared is None:
                raise ValueError("available alert channel requires its owner and prepared request")
            if decision.account_id != decision.prepared.account_id:
                raise ValueError("alert decision and prepared account differ")
            intents.append(
                OutboundIntent(
                    channel=decision.channel,
                    chat_id=decision.recipient,
                    agent_id=None,
                    source=OutboundSource(
                        kind=OutboundSourceKind.ALERT_GROUP, identity=str(group["id"]), block_idx=0
                    ),
                    prepared=decision.prepared,
                )
            )
        return tuple(intents)

    def pending_after(self, after: int, limit: int = 16) -> tuple[int, ...]:
        with self.outbound._pool().connection() as conn:
            rows = conn.execute(
                "SELECT g.id FROM alert_notification_groups g WHERE g.origin='native-v1' AND g.id>%s "
                "AND NOT EXISTS(SELECT 1 FROM im_bridge_alert_acceptances a WHERE a.group_id=g.id) "
                "ORDER BY g.id LIMIT %s",
                (after, limit),
            ).fetchall()
            return tuple(int(row[0]) for row in rows)


class AlertOutboundBridge:
    def __init__(
        self, outbound: IMOutboxStore, adapters: dict[str, IMAdapter], *, enabled: bool
    ) -> None:
        self.store = AlertOutboundStore(outbound)
        self.adapters = adapters
        self.enabled = enabled
        # A rotating scan hint, never a durable eligibility/acceptance watermark.
        self._scan_after = 0

    async def accept(self, group_id: int) -> AlertAcceptance:
        group, previous = await asyncio.to_thread(self.store.lookup, group_id)
        if previous is not None:
            return previous
        if not self.enabled or admission.quiesced():
            raise AlertAcceptanceHeldError("native alert acceptance is paused")
        decisions = tuple(
            [
                await self._prepare(channel, adapter, group["text"])
                for channel, adapter in sorted(self.adapters.items())
            ]
        )
        if admission.quiesced():
            raise AlertAcceptanceHeldError("native alert acceptance is paused")
        return await asyncio.to_thread(self.store.accept, group, decisions)

    @staticmethod
    async def _prepare(channel: str, adapter: IMAdapter, text: str) -> AlertRecipientDecision:
        try:
            account = await adapter.outbound_account_id()
        except Exception as exc:
            logger.warning(
                "native alert account held channel={} class={}", channel, type(exc).__name__
            )
            account = None
        if not account or not account.strip():
            return AlertRecipientDecision(
                channel=channel, account_id=None, decision=AlertOwnerDecision.ACCOUNT_UNAVAILABLE
            )
        try:
            recipient, prepared = await adapter.prepare_alert_owner(text)
        except NotImplementedError:
            reason = AlertOwnerDecision.UNSUPPORTED
        except SendNotStartedError:
            reason = AlertOwnerDecision.OWNER_UNAVAILABLE
        except Exception as exc:
            logger.warning(
                "native alert preparation held channel={} class={}", channel, type(exc).__name__
            )
            reason = AlertOwnerDecision.PREPARATION_UNAVAILABLE
        else:
            if recipient and prepared.account_id == account:
                return AlertRecipientDecision(
                    channel=channel,
                    account_id=account,
                    decision=AlertOwnerDecision.AVAILABLE,
                    recipient=recipient,
                    prepared=prepared,
                )
            reason = AlertOwnerDecision.PREPARATION_UNAVAILABLE
        return AlertRecipientDecision(channel=channel, account_id=account, decision=reason)

    async def poll_once(self) -> None:
        if not self.enabled or admission.quiesced():
            return
        pending = await asyncio.to_thread(self.store.pending_after, self._scan_after)
        for group_id in pending:
            if admission.quiesced():
                return
            try:
                await self.accept(group_id)
            except Exception as exc:
                logger.warning(
                    "native alert acceptance held group={} class={}", group_id, type(exc).__name__
                )
            self._scan_after = group_id
        if len(pending) < 16:
            self._scan_after = 0

    async def handle(self, body: bytes) -> tuple[int, bytes, str]:
        try:
            request = AlertOutboundRequest.model_validate_json(body)
        except ValidationError:
            return 400, b'{"error":"invalid native alert request"}', "application/json"
        try:
            accepted = await self.accept(request.group_id)
        except NativeAlertSourceError:
            return 404, b'{"error":"native alert source unavailable"}', "application/json"
        except OutboundIdentityConflictError:
            return 409, b'{"error":"native alert identity conflict"}', "application/json"
        except AlertAcceptanceHeldError:
            return 503, b'{"error":"native alert acceptance held"}', "application/json"
        except Exception as exc:
            logger.warning("native alert RPC held class={}", type(exc).__name__)
            return 503, b'{"error":"native alert acceptance held"}', "application/json"
        return 200, json.dumps(accepted.model_dump(mode="json")).encode(), "application/json"

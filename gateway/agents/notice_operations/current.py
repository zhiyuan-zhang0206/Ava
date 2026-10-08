"""Observed-row current-notice mutations and immutable acceptance snapshots."""

from dataclasses import dataclass
from typing import Annotated, LiteralString, cast

from fastapi import Header, HTTPException, Request
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from base.db.transaction import write_transaction
from gateway.agents.notice_operations.receipts import existing_receipt, save_receipt
from gateway.agents.schemas import NoticeEditIn, NoticeItem
from gateway.http.auth.request_principal import (
    PRINCIPAL_SCOPE,
    SCOPE_HEADER,
    AuthPrincipal,
    PrincipalScopeError,
    principal_key,
)


class ObservedNotice(BaseModel):
    """Target the positive global feed ID, never notify()'s zero-based local ID."""

    model_config = ConfigDict(extra="forbid")
    observed_notice_id: Annotated[
        int,
        Field(
            strict=True,
            gt=0,
            description="Global NoticeItem.id from the feed/inspector; not the SDK notify local id.",
        ),
    ]


class GuardedNoticeEdit(NoticeEditIn, ObservedNotice):
    """Edit explicitly supplied fields on the observed global notice row."""

    @model_validator(mode="after")
    def validate_changes(self) -> "GuardedNoticeEdit":
        changes = self.model_fields_set - {"observed_notice_id"}
        if not changes:
            raise ValueError("edit needs at least one field to change")
        if "title" in changes and (not self.title or not self.title.strip()):
            raise ValueError("title must be non-empty")
        for name in ("priority", "blocking"):
            if name in changes and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


@dataclass(frozen=True)
class NoticeAcceptance:
    record: NoticeItem
    replayed: bool


def operation_key(
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    idempotency_scope: str = Header(alias=SCOPE_HEADER),
) -> str:
    """Require a verified credential and explicit principal-v1 identity."""
    principal = getattr(request.state, "auth_principal", None)
    if idempotency_scope != PRINCIPAL_SCOPE or not isinstance(principal, AuthPrincipal):
        raise HTTPException(
            status_code=422, detail="guarded notices require verified principal-v1 scope"
        )
    try:
        return principal_key(principal, request.method, request.url.path, idempotency_key)
    except (PrincipalScopeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _target(conn: Connection, agent_id: int, observed_id: int) -> NoticeItem:
    # Serialize against creation's agent FOR UPDATE before acquiring notice rows.
    # NO KEY UPDATE permits the reply path's inbound FK KEY SHARE lock.
    agent = conn.execute(
        "SELECT id FROM agents WHERE id=%s FOR NO KEY UPDATE", (agent_id,)
    ).fetchone()
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT n.*, a.label AS agent_label FROM agent_notices n "
            "JOIN agents a ON a.id=n.agent_id "
            "WHERE n.id=%s AND n.agent_id=%s FOR UPDATE OF n",
            (observed_id, agent_id),
        )
        row = cur.fetchone()
    if row is None or row["resolved_at"] is not None:
        raise HTTPException(status_code=409, detail="observed notice is no longer current")
    return NoticeItem.model_validate(row)


def _snapshot(conn: Connection, notice_id: int) -> NoticeItem:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT n.*, a.label AS agent_label FROM agent_notices n "
            "JOIN agents a ON a.id=n.agent_id WHERE n.id=%s",
            (notice_id,),
        )
        return NoticeItem.model_validate(cur.fetchone())


def mutate(
    pool: ConnectionPool,
    path: str,
    key: str,
    agent_id: int,
    body: ObservedNotice,
) -> NoticeAcceptance:
    """Commit the observed mutation and receipt together, replay before mutable guards."""
    payload = body.model_dump(mode="json", exclude_unset=True)
    with write_transaction(pool) as conn:
        previous = existing_receipt(conn, path, key, payload)
        if previous is not None:
            return NoticeAcceptance(NoticeItem.model_validate(previous), replayed=True)
        record = _target(conn, agent_id, body.observed_notice_id)
        if isinstance(body, GuardedNoticeEdit):
            _edit(conn, record, body)
        else:
            conn.execute(
                "UPDATE agent_notices SET resolved_at=now(), resolution='withdrawn' WHERE id=%s",
                (record.id,),
            )
        updated = _snapshot(conn, record.id)
        save_receipt(conn, path, key, payload, updated.model_dump(mode="json"))
    return NoticeAcceptance(updated, replayed=False)


def _edit(conn: Connection, record: NoticeItem, body: GuardedNoticeEdit) -> None:
    if body.blocking and not record.require_response:
        raise HTTPException(
            status_code=422, detail="blocking=True requires a notice that needs a response"
        )
    changes = body.model_dump(exclude_unset=True, exclude={"observed_notice_id"})
    # Keys come exclusively from the validated, extra-forbidden model fields.
    columns = [name for name in ("title", "content", "priority", "blocking") if name in changes]
    assignments = ", ".join(f"{name}=%s" for name in columns)
    conn.execute(
        cast(
            LiteralString,
            "UPDATE agent_notices SET " + assignments + ", updated_at=now() WHERE id=%s",  # noqa: S608 -- fixed model fields only
        ),
        (*[changes[name] for name in columns], record.id),
    )

"""Status-only SQL boundaries retain the lease lifecycle vocabulary."""

import json
from typing import Any
from uuid import uuid4

import pytest
from pydantic import TypeAdapter

from base.agents.impersonation._store import public
from base.agents.impersonation.status import (
    OPEN,
    ImpersonationStatus,
    OpenImpersonationStatus,
    parse_lease,
)


@pytest.mark.parametrize("status", list(ImpersonationStatus))
def test_parse_lease_preserves_wire_values(status: ImpersonationStatus) -> None:
    row: dict[str, Any] = {"status": status.value, "id": uuid4(), "token_hash": "secret"}
    parsed = parse_lease(row)
    assert parsed["status"] is status
    rendered = json.loads(json.dumps(public(parsed)))
    assert rendered == {"status": status.value, "id": str(row["id"])}


@pytest.mark.parametrize("status", ["unexpected", "", None, 1])
def test_parse_lease_rejects_unknown_status(status: object) -> None:
    with pytest.raises(ValueError):
        parse_lease({"status": status})


def test_parse_lease_requires_status() -> None:
    with pytest.raises(KeyError):
        parse_lease({})


def test_open_projection_rejects_closed_statuses() -> None:
    adapter = TypeAdapter[OpenImpersonationStatus](OpenImpersonationStatus)
    for status in ImpersonationStatus:
        if status in OPEN:
            assert adapter.validate_python(status.value) is status
        else:
            with pytest.raises(ValueError):
                adapter.validate_python(status.value)


@pytest.mark.parametrize("row", [{}, {"status": "unexpected"}, {"status": None}])
@pytest.mark.parametrize("reader", ["public", "public_session", "resolve", "list_sessions"])
def test_public_readers_reject_invalid_raw_status(
    row: dict[str, Any], reader: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace
    from typing import cast

    from base.agents.impersonation import history, sessions
    from base.db import Database

    def execute(*_args: object) -> None:
        pass

    # Supply every projection field so an unrelated missing key cannot fake a rejection.
    fields = (
        "name",
        "executor_name",
        "process_metadata",
        "created_at",
        "activated_at",
        "ended_at",
        "expires_at",
        "ttl_seconds",
        "ack_window_seconds",
        "max_delivery_attempts",
        "reason",
        "summary",
        "handoff_path",
        "handoff_applied_at",
        "rejection_reason",
        "relay_provider",
        "relay_heartbeat_at",
        "relay_last_failure_at",
        "events_completed_at",
    )
    raw: dict[str, Any] = dict.fromkeys(fields)
    raw.update(agent_id=7, session_id=1, machine="local", automatic=False)
    raw.update(row)
    monkeypatch.setattr(history, "machine_name", lambda: "local")
    cursor = SimpleNamespace(execute=execute, fetchone=lambda: raw, fetchall=lambda: [raw])

    def context_cursor(**_kwargs: object) -> nullcontext[SimpleNamespace]:
        return nullcontext(cursor)

    connection = SimpleNamespace(cursor=context_cursor)
    db = cast(Database, SimpleNamespace(connect=lambda: nullcontext(connection)))
    expected = KeyError if "status" not in row else ValueError
    with pytest.raises(expected):
        if reader == "public":
            public(raw)
        elif reader == "public_session":
            history.public_session(raw)
        elif reader == "resolve":
            history.resolve(db, 7, 1)
        else:
            sessions.list_sessions(db, 7)

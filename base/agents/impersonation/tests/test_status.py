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

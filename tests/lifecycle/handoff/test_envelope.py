"""The frozen v1 envelope reader takes exact JSON types only.

Once a cutover image ships this reader it can never be tightened, so every
envelope field is held to its exact JSON spelling now: `version` is the JSON
integer 1 (not `true`, `1.0` or `"1"`), `id` is a canonical lowercase UUID
string, and every other field has its exact type. Fields the reader does not
know stay ignored by design: a later release adds request kinds and fields
without a second release (`test_cli_handoff`).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest

from base.api_contracts.release_handoff import HandoffRefusedError, read_envelope

_ID = "0f1e2d3c-4b5a-4968-8778-a6b5c4d3e2f1"
_EXECUTOR = {
    "artifact_digest": "a" * 64,
    "manifest_digest": "b" * 64,
    "schema_digest": "c" * 64,
    "source_commit": "d" * 40,
}
_ENVELOPE: dict[str, Any] = {
    "version": 1,
    "kind": "release",
    "id": _ID,
    "home": "/Users/zzy/.ava",
    "machine": "macbook-air",
    "executor": _EXECUTOR,
}


def _read(document: object) -> None:
    read_envelope(json.dumps(document).encode())


def test_the_exact_envelope_reads() -> None:
    envelope = read_envelope(json.dumps(_ENVELOPE).encode())
    assert envelope.version == 1
    assert envelope.id == UUID(_ID)


def test_fields_the_reader_does_not_know_are_ignored() -> None:
    later: dict[str, Any] = _ENVELOPE | {
        "units": [],
        "executor": _EXECUTOR | {"abi_tag": {"os": "later"}},
    }
    assert read_envelope(json.dumps(later).encode()).machine == "macbook-air"


@pytest.mark.parametrize("version", [True, 1.0, "1", 2, 0, False, None, [1]])
def test_the_version_is_exactly_the_json_integer_one(version: object) -> None:
    with pytest.raises(HandoffRefusedError, match="no v1 handoff envelope"):
        _read(_ENVELOPE | {"version": version})


@pytest.mark.parametrize(
    "identifier",
    [
        _ID.upper(),
        _ID.replace("-", ""),
        "{" + _ID + "}",
        "urn:uuid:" + _ID,
        int(UUID(_ID)),
    ],
)
def test_the_id_is_a_canonical_uuid_string(identifier: object) -> None:
    with pytest.raises(HandoffRefusedError, match="no v1 handoff envelope"):
        _read(_ENVELOPE | {"id": identifier})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", 1),
        ("home", 1),
        ("machine", 1),
        ("machine", True),
        ("executor", "a" * 64),
        ("executor", [_EXECUTOR]),
        ("executor", _EXECUTOR | {"source_commit": 1}),
    ],
)
def test_every_other_field_has_its_exact_type(field: str, value: object) -> None:
    with pytest.raises(HandoffRefusedError, match="no v1 handoff envelope"):
        _read(_ENVELOPE | {field: value})


@pytest.mark.parametrize("field", sorted(_ENVELOPE))
def test_a_missing_field_refuses(field: str) -> None:
    with pytest.raises(HandoffRefusedError, match="no v1 handoff envelope"):
        _read({key: value for key, value in _ENVELOPE.items() if key != field})


@pytest.mark.parametrize("field", sorted(_EXECUTOR))
def test_a_missing_executor_field_refuses(field: str) -> None:
    executor = {key: value for key, value in _EXECUTOR.items() if key != field}
    with pytest.raises(HandoffRefusedError, match="no v1 handoff envelope"):
        _read(_ENVELOPE | {"executor": executor})


def test_a_document_that_is_not_an_object_refuses() -> None:
    with pytest.raises(HandoffRefusedError, match="no v1 handoff envelope"):
        _read([_ENVELOPE])

"""Selector compatibility does not silently mint a publication input."""

import json

import pytest
from pydantic import ValidationError

from shared.managed_writer_observation import ExpectedUnitWriters
from shared.runtime_publication_input import (
    _published_unit,
    _receipt_expected,
    read_publication_selector,
    resolve_runtime_publication_input,
)


def test_source_runtime_has_no_publication_input() -> None:
    assert resolve_runtime_publication_input() is None


def test_exact_legacy_selector_has_no_receipt_binding() -> None:
    assert (
        read_publication_selector(
            json.dumps({"artifact_digest": "a" * 64, "manifest_digest": "b" * 64}).encode()
        )
        is None
    )


@pytest.mark.parametrize(
    "change", [{"version": 9}, {"prepared_receipt_digest": "bad"}, {"extra": True}]
)
def test_unknown_or_damaged_selector_refuses(change: dict[str, object]) -> None:
    raw = {
        "version": 2,
        "artifact_digest": "a" * 64,
        "manifest_digest": "b" * 64,
        "prepared_receipt_digest": "c" * 64,
    }
    with pytest.raises(ValidationError):
        read_publication_selector(json.dumps(raw | change).encode())


def test_published_unit_keeps_observer_and_full_receipt_digests_distinct() -> None:
    expected = ExpectedUnitWriters(
        machine="runner",
        home="/ava",
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        processes=(),
        sessions=(),
        launchers=(),
    )
    actual = _published_unit(expected, "c" * 64)

    assert actual.inventory_digest == expected.unit().inventory_digest
    assert actual.prepared_receipt_digest == "c" * 64
    assert actual.inventory_digest != actual.prepared_receipt_digest


def test_preparation_receipt_requires_the_classification_member() -> None:
    """Classified exclusions are a required receipt member, never an implicit empty."""
    expected = ExpectedUnitWriters(
        machine="runner",
        home="/ava",
        artifact_digest="a" * 64,
        manifest_digest="b" * 64,
        processes=(),
        sessions=(),
        launchers=(),
    )
    body: dict[str, object] = {
        "version": 1,
        "expected": expected.model_dump(mode="json"),
        "services": [{"session": "ava-ops", "requires_db": False, "gate": None}],
        "excluded_registrations": [
            {
                "label": "com.ava.machine.caffeinate",
                "definition_digest": "d" * 64,
                "classification": "machine",
            }
        ],
        "inventory_digest": expected.unit().inventory_digest,
        "closure": "unknown",
        "unresolved": [],
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()

    assert _receipt_expected(encoded) == expected
    del body["excluded_registrations"]
    without_member = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(ValidationError):
        _receipt_expected(without_member)

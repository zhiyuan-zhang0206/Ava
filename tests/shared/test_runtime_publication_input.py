"""Selector compatibility does not silently mint a publication input."""

import json

import pytest
from pydantic import ValidationError

from shared.runtime_publication_input import (
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
    "change", [{"version": 9}, {"inventory_receipt_digest": "bad"}, {"extra": True}]
)
def test_unknown_or_damaged_selector_refuses(change: dict[str, object]) -> None:
    raw = {
        "version": 2,
        "artifact_digest": "a" * 64,
        "manifest_digest": "b" * 64,
        "inventory_receipt_digest": "c" * 64,
    }
    with pytest.raises(ValidationError):
        read_publication_selector(json.dumps(raw | change).encode())

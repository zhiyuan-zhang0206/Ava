"""_unit_home() backs the default for path fields (pidfiles / memory / milvus /
logs) so they live under THIS unit's home and follow a ~/.ava -> ~/.ava_gateway
rename instead of pinning to ~/.ava.

AVA_HOME is a Settings alias, so monkeypatch.setenv on it is banned by the
force-settings lint (it would no-op the module-load singleton). _unit_home reads
os.environ directly at field-construction time, so these tests set os.environ
directly (allowed in tests) with explicit restore."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from shared.config._base import _unit_home


@pytest.fixture
def _restore_ava_home() -> Iterator[None]:
    orig = os.environ.get("AVA_HOME")
    try:
        yield
    finally:
        if orig is None:
            os.environ.pop("AVA_HOME", None)
        else:
            os.environ["AVA_HOME"] = orig


def test_unit_home_follows_ava_home(_restore_ava_home: None) -> None:
    os.environ["AVA_HOME"] = "/srv/.ava_gateway"
    assert _unit_home() == Path("/srv/.ava_gateway")


def test_unit_home_defaults_to_home_ava(_restore_ava_home: None) -> None:
    os.environ.pop("AVA_HOME", None)
    assert _unit_home() == Path.home() / ".ava"


def test_pidfile_fields_rooted_under_unit_home(_restore_ava_home: None) -> None:
    """A fresh Settings under AVA_HOME roots pidfiles / memory / milvus / logs
    beneath it, not under ~/.ava."""
    os.environ["AVA_HOME"] = "/srv/.ava_gateway"
    from shared.config import Settings

    s = Settings()
    root = Path("/srv/.ava_gateway")
    assert s.services.restarter_pidfile == root / "run" / "restarter.pid"
    assert s.services.gateway_pidfile == root / "run" / "gateway.pid"
    assert s.services.memory_root == root / "memory"
    assert s.services.milvus_data_dir == root / "milvus-data"
    assert s.services.memory_search_data_dir == root / "memory-search"

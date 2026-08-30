"""shared.test_db_guard — the fail-fast non-test-DB rule.

Covers the rule added after the 2026-08-12 incident (a test run rooted
outside this repo resolved the operator's real ~/.ava/.env and seeded
synthetic agents 900002-900010 into the production agents table). The rule is
the single source of truth for "is this database one a test process may
write"; every pytest bootstrap calls it at session start and the DB-seeding
test helpers call it before connecting.
"""

from __future__ import annotations

import pytest

from shared.test_db_guard import assert_test_db_url


def test_main_cluster_db_always_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """ava_main is the main cluster's database — refused even with the marker."""
    monkeypatch.delenv("AVA_TEST_DB", raising=False)
    with pytest.raises(RuntimeError, match="production database"):
        assert_test_db_url(
            "postgresql://ava_main:***@10.0.0.2:6433/ava_main",
            context="test",
        )
    monkeypatch.setenv("AVA_TEST_DB", "1")
    with pytest.raises(RuntimeError, match="production database"):
        assert_test_db_url("postgresql://ava_main@127.0.0.1:6433/ava_main", context="test")


def test_main_cluster_hostname_always_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname carrying ava_main is refused regardless of the db name."""
    monkeypatch.setenv("AVA_TEST_DB", "1")
    with pytest.raises(RuntimeError, match="production database"):
        assert_test_db_url(
            "postgresql://x@ava_main-gw.example.internal:6433/somedb",
            context="test",
        )


def test_fresh_birth_name_needs_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """'ava' is the fresh-birth cluster name (worktree dev clusters too) —
    a test process may target it, but only with the explicit marker."""
    monkeypatch.delenv("AVA_TEST_DB", raising=False)
    with pytest.raises(RuntimeError, match="AVA_TEST_DB"):
        assert_test_db_url("postgresql://ava@127.0.0.1:5433/ava", context="test")
    monkeypatch.setenv("AVA_TEST_DB", "1")
    assert assert_test_db_url("postgresql://ava@127.0.0.1:5433/ava", context="test") is None


def test_throwaway_names_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite's provisioned throwaway databases pass without any marker."""
    monkeypatch.delenv("AVA_TEST_DB", raising=False)
    assert assert_test_db_url("postgresql://ava@127.0.0.1:54321/ava_citest", context="test") is None
    assert (
        assert_test_db_url("postgresql://ava@127.0.0.1:54322/ava_test_42", context="test") is None
    )


def test_unprovisioned_sentinel_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The import-time sentinel (nothing listens on port 1) passes — any write
    against it fails loudly on its own."""
    monkeypatch.delenv("AVA_TEST_DB", raising=False)
    assert (
        assert_test_db_url("postgresql://unprovisioned@127.0.0.1:1/unprovisioned", context="test")
        is None
    )


def test_unknown_name_needs_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any other database name is fail-closed: refused without the marker."""
    monkeypatch.delenv("AVA_TEST_DB", raising=False)
    with pytest.raises(RuntimeError, match="AVA_TEST_DB"):
        assert_test_db_url("postgresql://u@127.0.0.1:5433/mydb", context="test")
    monkeypatch.setenv("AVA_TEST_DB", "1")
    assert assert_test_db_url("postgresql://u@127.0.0.1:5433/mydb", context="test") is None


def test_missing_url_refused() -> None:
    with pytest.raises(RuntimeError, match="not set"):
        assert_test_db_url(None, context="test")


def test_error_carries_context() -> None:
    with pytest.raises(RuntimeError, match="my-seed-helper"):
        assert_test_db_url(
            "postgresql://ava_main@127.0.0.1:6433/ava_main",
            context="my-seed-helper",
        )

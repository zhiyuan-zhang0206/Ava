"""Durable --disable-service marker — the bridge that keeps an operator's
`ava start --disable-service X` honored by the watchdog. See
shared/disabled_services.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shared import disabled_services as ds


@pytest.fixture(autouse=True)
def _marker_in_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the marker at a per-test tmp file (default resolves under $AVA_HOME)."""
    monkeypatch.setattr(ds, "disabled_services_file", lambda: tmp_path / "disabled_services")


def test_read_absent_marker_is_empty() -> None:
    assert ds.read_skipped() == set()


def test_write_then_read_roundtrips() -> None:
    ds.write_skipped({"labeler", "memory-indexer"})
    assert ds.read_skipped() == {"labeler", "memory-indexer"}


def test_write_empty_clears() -> None:
    ds.write_skipped({"labeler"})
    assert ds.read_skipped() == {"labeler"}
    ds.write_skipped(set())
    assert ds.read_skipped() == set()


def test_names_normalize_across_kebab_and_snake() -> None:
    # session name is kebab (memory-indexer); the watchdog check name is snake
    # (memory_indexer). Both must compare equal so a skip is not silently missed.
    ds.write_skipped({"memory_indexer"})
    assert ds.read_skipped() == {"memory-indexer"}
    assert ds.is_skipped("memory_indexer", ds.read_skipped())
    assert ds.is_skipped("memory-indexer", ds.read_skipped())


def test_resolve_launch_skip_persist_writes_marker() -> None:
    """Operator start: persist=True records the durable intent and returns it."""
    launch = ds.resolve_launch_skip({"labeler"}, persist=True)
    assert launch == {"labeler"}
    assert ds.read_skipped() == {"labeler"}


def test_resolve_launch_skip_no_persist_reads_and_unions_without_writing() -> None:
    """Internal restart: persist=False keeps the marker, returns marker ∪ transient."""
    ds.write_skipped({"labeler"})  # operator's durable intent already on disk
    launch = ds.resolve_launch_skip({"frontend"}, persist=False)
    assert launch == {"labeler", "frontend"}  # union for this launch
    assert ds.read_skipped() == {"labeler"}  # marker unchanged — frontend was transient


# --- pre-rename marker migration ------------------------------------------
#
# `skipped_services` was renamed to `disabled_services` with no migration, so a
# durable disable recorded before the rename went unread — the services came back
# on and nothing said so. These pin both directions plus idempotence. Each test
# builds its own home under tmp_path; nothing here can reach the real ~/.ava.


def test_marker_name_matches_paths_helper() -> None:
    """The spelled-out basename must stay equal to the path helper's — the
    migration derives the live path from the constant, readers from the helper."""
    from shared.paths import disabled_services_file

    assert disabled_services_file().name == ds._MARKER_NAME


def _home(tmp_path: Path, name: str) -> Path:
    home = tmp_path / name
    home.mkdir()
    return home


def test_migrate_promotes_legacy_marker_when_new_name_absent(tmp_path: Path) -> None:
    """Only the old file on disk -> it becomes the live marker, intent honored."""
    home = _home(tmp_path, "promote")
    (home / "skipped_services").write_text("browser\nfrontend\n")

    summary = ds.migrate_legacy_marker(home)

    assert not (home / "skipped_services").exists()
    assert ds._read_names(home / "disabled_services") == {"browser", "frontend"}
    assert summary is not None
    assert "browser, frontend" in summary


def test_migrate_is_noop_without_a_legacy_marker(tmp_path: Path) -> None:
    """The steady state — nothing to do, nothing said, live marker untouched."""
    home = _home(tmp_path, "steady")
    (home / "disabled_services").write_text("labeler\n")

    assert ds.migrate_legacy_marker(home) is None
    assert (home / "disabled_services").read_text() == "labeler\n"


def test_migrate_keeps_new_name_authoritative_and_archives_the_old(tmp_path: Path) -> None:
    """Both files -> the new one wins (it is what current code writes, so it is the
    operator's later word) and the old one survives as evidence, not as silence."""
    home = _home(tmp_path, "conflict")
    (home / "disabled_services").write_text("labeler\n")
    (home / "skipped_services").write_text("browser\nfrontend\n")

    summary = ds.migrate_legacy_marker(home)

    assert ds._read_names(home / "disabled_services") == {"labeler"}  # untouched
    assert not (home / "skipped_services").exists()
    assert (home / "skipped_services.superseded").read_text() == "browser\nfrontend\n"
    assert summary is not None
    assert "labeler" in summary and "browser, frontend" in summary
    assert "NOT applied" in summary


def test_migrate_keeps_an_empty_new_marker_authoritative(tmp_path: Path) -> None:
    """The field case (Windows runner): a 0-byte `disabled_services` beside a
    legacy file. Empty is a real value here — an `ava start` with no
    `--disable-service` — so it wins, and the legacy names are reported, not applied."""
    home = _home(tmp_path, "empty-live")
    (home / "disabled_services").write_text("")
    (home / "skipped_services").write_text("browser\nbrowser-mcp\nfrontend\n")

    summary = ds.migrate_legacy_marker(home)

    assert ds._read_names(home / "disabled_services") == set()
    assert (home / "skipped_services.superseded").exists()
    assert summary is not None
    assert "(none)" in summary


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    """Converge runs constantly: a second pass must neither undo the promotion nor
    duplicate anything."""
    home = _home(tmp_path, "twice")
    (home / "skipped_services").write_text("browser\n")

    first = ds.migrate_legacy_marker(home)
    second = ds.migrate_legacy_marker(home)

    assert first is not None
    assert second is None  # nothing left to migrate
    assert ds._read_names(home / "disabled_services") == {"browser"}
    assert not (home / "skipped_services.superseded").exists()


def test_migrate_archive_pass_is_idempotent(tmp_path: Path) -> None:
    """Same for the both-files branch — the archive is written once and the live
    marker keeps its value."""
    home = _home(tmp_path, "twice-conflict")
    (home / "disabled_services").write_text("labeler\n")
    (home / "skipped_services").write_text("browser\n")

    ds.migrate_legacy_marker(home)
    assert ds.migrate_legacy_marker(home) is None
    assert ds._read_names(home / "disabled_services") == {"labeler"}
    assert (home / "skipped_services.superseded").read_text() == "browser\n"


def test_migrate_promotes_an_empty_legacy_marker(tmp_path: Path) -> None:
    """An empty legacy file is still an explicit "nothing disabled" — promote it so
    the step terminates instead of finding it again every round."""
    home = _home(tmp_path, "empty-legacy")
    (home / "skipped_services").write_text("")

    summary = ds.migrate_legacy_marker(home)

    assert (home / "disabled_services").exists()
    assert ds._read_names(home / "disabled_services") == set()
    assert summary is not None and "(none)" in summary

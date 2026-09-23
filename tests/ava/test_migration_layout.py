"""Tracked migration layout and preflight validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.migrations import (
    MigrationLayoutError,
    _list_migration_files,
    validate_migration_layout,
    validate_migrations_at_ref,
)
from tests.ava.migration_support import (
    _SYN,
    _SYN2,
    _git,
    _init_repo,
)
from tests.ava.migration_support import (
    _reset_schema_migrations_state as _reset_schema_migrations_state,
)


class TestLayoutValidation:
    def test_dir_missing_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path / "nope")
        with pytest.raises(MigrationLayoutError, match="does not exist"):
            _list_migration_files()

    def test_bad_filename_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / f"{_SYN}.sql").write_text("-- noop")
        (tmp_path / "0001_legacy.sql").write_text("-- noop")  # old integer format
        _init_repo(tmp_path)  # tracked: layout validation applies to git-tracked files
        monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
        with pytest.raises(MigrationLayoutError, match="does not match"):
            _list_migration_files()

    def test_readme_and_down_and_dotfiles_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / f"{_SYN}.sql").write_text("-- noop")
        (tmp_path / f"{_SYN}.down.sql").write_text("-- noop")
        (tmp_path / "README.md").write_text("docs")
        (tmp_path / ".DS_Store").write_text("junk")
        _init_repo(tmp_path)
        monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)
        assert [n for n, _ in _list_migration_files()] == [_SYN]


class TestValidateMigrationLayout:
    def test_good_names_pass(self) -> None:
        validate_migration_layout([f"{_SYN}.sql", f"{_SYN2}.sql"])

    def test_empty_is_valid(self) -> None:
        validate_migration_layout([".DS_Store", "README.md", f"{_SYN}.down.sql"])

    def test_duplicate_raises(self) -> None:
        with pytest.raises(MigrationLayoutError, match="duplicate migration name"):
            validate_migration_layout([f"{_SYN}.sql", f"{_SYN}.sql"])

    def test_bad_filename_raises(self) -> None:
        with pytest.raises(MigrationLayoutError, match="does not match"):
            validate_migration_layout([f"{_SYN}.sql", "0049_event_log.sql"])


class TestValidateMigrationsAtRef:
    def _init_repo(self, repo: Path, names: list[str]) -> None:
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
        (repo / "migrations").mkdir()
        for name in names:
            (repo / "migrations" / name).write_text("-- noop\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")

    def test_good_ref_passes(self, tmp_path: Path) -> None:
        self._init_repo(tmp_path, [f"{_SYN}.sql", f"{_SYN2}.sql"])
        validate_migrations_at_ref("HEAD", repo_root=tmp_path)

    def test_duplicate_in_ref_raises(self, tmp_path: Path) -> None:
        self._init_repo(tmp_path, [f"{_SYN}.sql", "sub_dir_placeholder.sql"])
        # a malformed name at the ref is refused before any service is stopped
        with pytest.raises(MigrationLayoutError, match="does not match"):
            validate_migrations_at_ref("HEAD", repo_root=tmp_path)

    def test_unreadable_ref_raises(self, tmp_path: Path) -> None:
        self._init_repo(tmp_path, [f"{_SYN}.sql"])
        with pytest.raises(MigrationLayoutError, match="cannot read migrations/"):
            validate_migrations_at_ref("no-such-ref", repo_root=tmp_path)

from __future__ import annotations

from pathlib import Path

import pytest

from base.deploy.schema import migration_layout
from base.deploy.schema.migration_layout import MIGRATIONS_DIR
from base.deploy.schema.migrations import (
    MigrationLayoutError,
    list_migration_files,
    validate_migration_layout,
    validate_migrations_at_ref,
)
from base.deploy.schema.tests.migration_support import (
    SYN,
    SYN2,
    git,
    init_repo,
)
from base.deploy.schema.tests.migration_support import (
    reset_schema_migrations_state as reset_schema_migrations_state,
)


def test_migrations_dir_resolves_beside_the_checkout_root() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    assert repo_root / "migrations" == MIGRATIONS_DIR
    assert MIGRATIONS_DIR.is_dir()


class TestLayoutValidation:
    def test_dir_missing_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(migration_layout, "MIGRATIONS_DIR", tmp_path / "nope")
        with pytest.raises(MigrationLayoutError, match="does not exist"):
            list_migration_files()

    def test_bad_filename_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / f"{SYN}.sql").write_text("-- noop")
        (tmp_path / "0001_legacy.sql").write_text("-- noop")
        init_repo(tmp_path)
        monkeypatch.setattr(migration_layout, "MIGRATIONS_DIR", tmp_path)
        with pytest.raises(MigrationLayoutError, match="does not match"):
            list_migration_files()

    def test_readme_and_dotfiles_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / f"{SYN}.sql").write_text("-- noop")
        (tmp_path / "README.md").write_text("docs")
        (tmp_path / ".DS_Store").write_text("junk")
        init_repo(tmp_path)
        monkeypatch.setattr(migration_layout, "MIGRATIONS_DIR", tmp_path)
        assert [n for n, _ in list_migration_files()] == [SYN]


class TestValidateMigrationLayout:
    def test_good_names_pass(self) -> None:
        validate_migration_layout([f"{SYN}.sql", f"{SYN2}.sql"])

    def test_empty_is_valid(self) -> None:
        validate_migration_layout([".DS_Store", "README.md"])

    def test_down_file_is_not_a_migration_name(self) -> None:
        with pytest.raises(MigrationLayoutError, match="does not match"):
            validate_migration_layout([f"{SYN}.down.sql"])

    def test_duplicate_raises(self) -> None:
        with pytest.raises(MigrationLayoutError, match="duplicate migration name"):
            validate_migration_layout([f"{SYN}.sql", f"{SYN}.sql"])

    def test_bad_filename_raises(self) -> None:
        with pytest.raises(MigrationLayoutError, match="does not match"):
            validate_migration_layout([f"{SYN}.sql", "0049_event_log.sql"])


class TestValidateMigrationsAtRef:
    def _init_repo(self, repo: Path, names: list[str]) -> None:
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "t@t")
        git(repo, "config", "user.name", "t")
        (repo / "migrations").mkdir()
        for name in names:
            (repo / "migrations" / name).write_text("-- noop\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "init")

    def test_good_ref_passes(self, tmp_path: Path) -> None:
        self._init_repo(tmp_path, [f"{SYN}.sql", f"{SYN2}.sql"])
        validate_migrations_at_ref("HEAD", repo_root=tmp_path)

    def test_duplicate_in_ref_raises(self, tmp_path: Path) -> None:
        self._init_repo(tmp_path, [f"{SYN}.sql", "sub_dir_placeholder.sql"])
        with pytest.raises(MigrationLayoutError, match="does not match"):
            validate_migrations_at_ref("HEAD", repo_root=tmp_path)

    def test_unreadable_ref_raises(self, tmp_path: Path) -> None:
        self._init_repo(tmp_path, [f"{SYN}.sql"])
        with pytest.raises(MigrationLayoutError, match="cannot read migrations/"):
            validate_migrations_at_ref("no-such-ref", repo_root=tmp_path)


def test_nested_migrations_are_sorted_by_name_not_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths: list[Path] = []
    for directory, name in (("z/early", SYN), ("a/later", SYN2)):
        path = tmp_path / directory / f"{name}.sql"
        path.parent.mkdir(parents=True)
        path.write_text("SELECT 1;")
        paths.append(path)
    init_repo(tmp_path)
    monkeypatch.setattr(migration_layout, "MIGRATIONS_DIR", tmp_path)
    assert list_migration_files() == list(zip((SYN, SYN2), paths, strict=True))


def test_duplicate_migration_name_in_separate_groups_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for directory in ("one", "two"):
        path = tmp_path / directory / f"{SYN}.sql"
        path.parent.mkdir()
        path.write_text("SELECT 1;")
    init_repo(tmp_path)
    monkeypatch.setattr(migration_layout, "MIGRATIONS_DIR", tmp_path)
    with pytest.raises(MigrationLayoutError, match="duplicate migration name"):
        list_migration_files()

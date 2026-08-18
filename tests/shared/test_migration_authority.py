"""`_assert_migration_authority` — only a cluster's gateway unit may migrate it.

The 2026-07-31 wedge: an agent-runner sharing the central DB pulled main ahead of
the gateway, and `ava start` step 2.5 applied the pending migrations to prod. The
gateway, still pinned at the older commit, then failed its own startup schema
check and rejected every agent boot.

The identity the DB carries is `machine_units` (gateway-capable rows, written by
`register_self`); the identity the executing side claims is
`checkout_anchored_home()` — deliberately NOT the env-resolved home, since a
worktree process that inherited `AVA_HOME=~/.ava` has a prod DB URL *and* a
prod-looking `ava_home()`, so only the checkout's own claim separates them.

The exemptions that must keep working are covered here too: a fresh birth (no
identity recorded yet), and a non-gateway host whose apply is a no-op (an
agent-runner's ordinary `ava start`).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from shared.config import settings
from shared.migrations import MigrationAuthorityMismatch, apply_pending_migrations

# This host is the gateway of the DB under test in the "matching" cases.
_GATEWAY = ("gateway-host", "/Users/ava/.ava")
# A different unit of the same cluster — the agent-runner that caused the wedge.
_RUNNER = ("wsl", "/home/ava/.ava")

_SYN = "29991231T235959_synthetic-authority"


@pytest.fixture(autouse=True)
def _clean_units_and_synthetic() -> Iterator[None]:
    """Own machine_units + the synthetic migration's row/table for each test.

    conftest's TRUNCATE list does not cover machine_units (it is cluster
    topology, not business data), so this module clears it before and after
    rather than inheriting whatever another module registered.
    """

    def _clear() -> None:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
            conn.execute("DELETE FROM machine_units")
            conn.execute("DELETE FROM schema_migrations WHERE name = %s", (_SYN,))
            conn.execute("DROP TABLE IF EXISTS syn_authority_t")

    _clear()
    yield
    _clear()


def _register_gateway_unit(machine: str, home: str) -> None:
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO machine_units "
            "(machine_name, home, serve_gateway, serve_agent_runner) "
            "VALUES (%s, %s, true, true)",
            (machine, home),
        )


def _claim_checkout(
    monkeypatch: pytest.MonkeyPatch, machine: str, home: str, *, anchored: bool = True
) -> None:
    """Make the executing checkout claim `machine:home`."""
    monkeypatch.setattr("shared.migrations.machine_name", lambda: machine)
    monkeypatch.setattr("shared.migrations.checkout_anchored_home", lambda: (Path(home), anchored))


def _as_git_worktree(tmp_path: Path) -> None:
    """Commit whatever is in tmp_path so the loader's git-tracking gate (#998)
    sees a real checkout — untracked migration files are skipped, so a fixture
    pointing MIGRATIONS_DIR at tmp_path must model one."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )


def _pending_migration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point MIGRATIONS_DIR at one synthetic post-baseline migration (tracked)."""
    (tmp_path / f"{_SYN}.sql").write_text("CREATE TABLE syn_authority_t (id int);")
    (tmp_path / f"{_SYN}.down.sql").write_text("DROP TABLE syn_authority_t;")
    _as_git_worktree(tmp_path)
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)


def _synthetic_table_exists() -> bool:
    with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
        row = conn.execute("SELECT to_regclass('syn_authority_t')").fetchone()
    return row is not None and row[0] is not None


def test_refuses_when_another_unit_owns_the_cluster(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The incident itself: the runner's checkout has a migration the gateway
    lacks, and applying it would strand the gateway behind the schema."""
    _register_gateway_unit(*_GATEWAY)
    _claim_checkout(monkeypatch, *_RUNNER)
    _pending_migration(monkeypatch, tmp_path)

    with (
        psycopg.connect(settings.data_plane.db_url) as conn,
        pytest.raises(MigrationAuthorityMismatch) as exc,
    ):
        apply_pending_migrations(conn)

    # The message must name both identities — the operator's first question is
    # "which host am I on, and which one owns this DB".
    assert "wsl:/home/ava/.ava" in str(exc.value)
    assert "gateway-host:/Users/ava/.ava" in str(exc.value)
    assert not _synthetic_table_exists(), "refusal must not have touched the schema"


def test_allows_a_fresh_birth_with_no_recorded_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A brand-new cluster migrates at `ava start` step 2.5, before step 3 writes
    machine_units — an empty table means "no owner yet", not "not you"."""
    _claim_checkout(monkeypatch, *_RUNNER)
    _pending_migration(monkeypatch, tmp_path)

    with psycopg.connect(settings.data_plane.db_url) as conn:
        assert apply_pending_migrations(conn) == [_SYN]
    assert _synthetic_table_exists()


def test_allows_the_gateway_unit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The sanctioned path — `ava cluster update`'s migrate step on the cluster's own
    gateway — is unaffected."""
    _register_gateway_unit(*_GATEWAY)
    _claim_checkout(monkeypatch, *_GATEWAY)
    _pending_migration(monkeypatch, tmp_path)

    with psycopg.connect(settings.data_plane.db_url) as conn:
        assert apply_pending_migrations(conn) == [_SYN]
    assert _synthetic_table_exists()


def test_allows_a_non_gateway_host_with_nothing_to_apply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An agent-runner's ordinary `ava start` calls this against the central DB
    and legitimately applies nothing. Authority is checked only when something
    would actually be written, so the guard must not turn every runner start into
    a hard failure."""
    _register_gateway_unit(*_GATEWAY)
    _claim_checkout(monkeypatch, *_RUNNER)
    _as_git_worktree(tmp_path)  # empty migrations/ in a real checkout
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)  # empty

    with psycopg.connect(settings.data_plane.db_url) as conn:
        assert apply_pending_migrations(conn) == []


def test_refuses_an_unanchored_checkout_even_at_the_owning_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dev worktree with no `.ava_home` pointer falls back to ~/.ava, which
    *looks* like the gateway's home. Ownership is a claim it cannot make, so the
    unanchored flag alone must refuse — this is the inherited-`AVA_HOME` path
    that reaches prod with a worktree's migrations."""
    _register_gateway_unit(*_GATEWAY)
    _claim_checkout(monkeypatch, _GATEWAY[0], _GATEWAY[1], anchored=False)
    _pending_migration(monkeypatch, tmp_path)

    with (
        psycopg.connect(settings.data_plane.db_url) as conn,
        pytest.raises(MigrationAuthorityMismatch) as exc,
    ):
        apply_pending_migrations(conn)

    assert "unanchored checkout" in str(exc.value)
    assert not _synthetic_table_exists()


def test_untracked_migration_files_names_only_untracked_sql(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The converge warning surfaces exactly what the loader skips: untracked
    `.sql` up-files — nothing else (tracked files, down files, dotfiles)."""
    (tmp_path / "20260808T010000_tracked.sql").write_text("-- up")
    _as_git_worktree(tmp_path)
    # Written AFTER the commit, so git does not track them:
    (tmp_path / "20260808T020000_untracked.sql").write_text("-- up")
    (tmp_path / "20260808T030000_untracked.down.sql").write_text("-- down")
    (tmp_path / ".hidden.sql").write_text("-- hidden")
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)

    from shared import migrations as m

    assert m.untracked_migration_files() == ["20260808T020000_untracked.sql"]


def test_untracked_migration_files_empty_outside_a_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Not a git worktree → empty, not an error: the loader fails closed there
    (nothing would be applied), so the warning surface has nothing to add."""
    (tmp_path / "20260808T010000_x.sql").write_text("-- up")
    monkeypatch.setattr("shared.migrations.MIGRATIONS_DIR", tmp_path)

    from shared import migrations as m

    assert m.untracked_migration_files() == []

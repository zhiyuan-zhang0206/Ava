"""`_assert_migration_authority` — only a cluster's gateway unit may migrate it.

The 2026-07-31 wedge: an agent-runner sharing the central DB pulled main ahead of
the gateway, and `ava start` step 2.5 applied the pending migrations to prod. The
gateway, still pinned at the older commit, then failed its own startup schema
check and rejected every agent boot.

The identity the DB carries is `machine_units` (gateway-capable rows, written by
`register_self`); the identity the executing side claims is
its home (`resolve_ava_home()`) together with the rule that a home carrying its
own `<home>/source` checkout is changed only by that checkout: a worktree process
that inherited `AVA_HOME=~/.ava` has a prod DB URL *and* a prod-looking
`ava_home()`, so the checkout rule is what separates them.

The exemptions that must keep working are covered here too: a fresh birth (no
identity recorded yet), and a non-gateway host whose apply is a no-op (an
agent-runner's ordinary `ava start`).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from base.config import settings
from base.deploy.schema.migrations import MigrationAuthorityMismatch, apply_pending_migrations

# This host is the gateway of the DB under test in the "matching" cases.
_GATEWAY = ("gateway-host", "/Users/ava/.ava")
# A different unit of the same cluster — the agent-runner that caused the wedge.
_RUNNER = ("wsl", "/home/ava/.ava")

_SYN = "29991231T235959_synthetic-authority"


@pytest.fixture(autouse=True)
def _clean_units_and_synthetic() -> Iterator[None]:
    """Own machine_units + the synthetic migration's row/table for each test,
    and put back the applied names this module's applying tests squash away.

    conftest's TRUNCATE list does not cover machine_units (it is cluster
    topology, not business data), so this module clears it before and after
    rather than inheriting whatever another module registered.

    schema_migrations needs the same ownership. The tests that point
    MIGRATIONS_DIR at a tmp dir holding only the synthetic migration make every
    REAL migration name a squash candidate, and a squash DELETEs those rows from
    the suite's shared database for the rest of the session. Left unrestored,
    what the next test sees depends on which sibling ran first — an ordering
    xdist is free to change, and did: CI shard 7 failed while the same test
    passed locally.
    """
    from base.deploy.schema.migrations import required_migration_set

    # Snapshot before any test monkeypatches MIGRATIONS_DIR, so teardown
    # restores the checkout's real set rather than some tmp dir's.
    required = sorted(required_migration_set())

    def _clear() -> None:
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as conn:
            conn.execute("DELETE FROM machine_units")
            conn.execute("DELETE FROM schema_migrations WHERE name = %s", (_SYN,))
            conn.execute("DROP TABLE IF EXISTS syn_authority_t")
            for name in required:
                conn.execute(
                    "INSERT INTO schema_migrations (name) VALUES (%s) ON CONFLICT DO NOTHING",
                    (name,),
                )

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


def _claim_checkout(monkeypatch: pytest.MonkeyPatch, machine: str, home: str) -> None:
    """Make the executing process claim `machine:home`."""
    monkeypatch.setattr("base.deploy.schema.migrations.machine_name", lambda: machine)
    monkeypatch.setenv("AVA_HOME", home)


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
    _as_git_worktree(tmp_path)
    monkeypatch.setattr("base.deploy.schema.migrations.MIGRATIONS_DIR", tmp_path)


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
    """The sanctioned path — the fleet update's migrate step on the cluster's own
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
    a hard failure.

    Runs against the checkout's REAL migrations dir, which is what makes this a
    genuine no-op: the suite provisions its database with exactly those
    migrations applied, so nothing is pending and nothing is squashed. Pointing
    MIGRATIONS_DIR at an empty dir instead would make every applied migration a
    squash candidate — a mutation, which the guard is supposed to refuse.
    """
    _register_gateway_unit(*_GATEWAY)
    _claim_checkout(monkeypatch, *_RUNNER)

    with psycopg.connect(settings.data_plane.db_url) as conn:
        assert apply_pending_migrations(conn) == []


def test_refuses_a_foreign_checkout_even_at_the_owning_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dev worktree that inherited the gateway's `AVA_HOME` resolves the very
    home the database names, yet its `migrations/` are not the ones the home's own
    `source` checkout carries. The checkout rule alone must refuse — this is the
    inherited-`AVA_HOME` path that reaches prod with a worktree's migrations."""
    home = tmp_path / "gateway-home"
    (home / "source").mkdir(parents=True)
    _register_gateway_unit(_GATEWAY[0], str(home))
    _claim_checkout(monkeypatch, _GATEWAY[0], str(home))
    _pending_migration(monkeypatch, tmp_path)

    with (
        psycopg.connect(settings.data_plane.db_url) as conn,
        pytest.raises(MigrationAuthorityMismatch) as exc,
    ):
        apply_pending_migrations(conn)

    assert "foreign checkout" in str(exc.value)
    assert not _synthetic_table_exists()


def test_untracked_migration_files_names_only_untracked_sql(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The converge warning surfaces exactly what the loader skips: untracked
    `.sql` up-files — nothing else (tracked files, dotfiles)."""
    (tmp_path / "20260808T010000_tracked.sql").write_text("-- up")
    _as_git_worktree(tmp_path)
    # Written AFTER the commit, so git does not track them:
    (tmp_path / "20260808T020000_untracked.sql").write_text("-- up")
    (tmp_path / ".hidden.sql").write_text("-- hidden")
    monkeypatch.setattr("base.deploy.schema.migrations.MIGRATIONS_DIR", tmp_path)

    from base.deploy.schema import migrations as m

    assert m.untracked_migration_files() == ["20260808T020000_untracked.sql"]


def test_untracked_migration_files_empty_outside_a_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Not a git worktree → empty, not an error: the loader fails closed there
    (nothing would be applied), so the warning surface has nothing to add."""
    (tmp_path / "20260808T010000_x.sql").write_text("-- up")
    monkeypatch.setattr("base.deploy.schema.migrations.MIGRATIONS_DIR", tmp_path)

    from base.deploy.schema import migrations as m

    assert m.untracked_migration_files() == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through mode 0")
def test_unreadable_migration_files_names_a_denied_tracked_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The apply-side vet: a tracked migration the applier cannot open is named
    before an update stops the host (`validate_migrations_at_ref` vets names)."""
    locked = tmp_path / "20260808T010000_locked.sql"
    locked.write_text("-- up")
    _as_git_worktree(tmp_path)
    locked.chmod(0)
    monkeypatch.setattr("base.deploy.schema.migrations.MIGRATIONS_DIR", tmp_path)

    from base.deploy.schema import migrations as m

    problems = m.unreadable_migration_files()
    assert [name for name, _ in problems] == ["20260808T010000_locked"]
    assert problems[0][1]


def test_unreadable_migration_files_empty_when_every_tracked_file_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "20260808T010000_ok.sql").write_text("-- up")
    _as_git_worktree(tmp_path)
    monkeypatch.setattr("base.deploy.schema.migrations.MIGRATIONS_DIR", tmp_path)

    from base.deploy.schema import migrations as m

    assert m.unreadable_migration_files() == []

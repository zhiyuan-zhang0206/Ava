"""Re-affirming `ava_runner`'s read grants when a migration grows the schema.

`GRANT SELECT ON ALL TABLES IN SCHEMA public` is a point-in-time loop over the
tables that exist when it runs — Postgres expands it into per-object ACL entries
and nothing carries forward. It runs once, at install birth. So every table a
later migration creates is invisible to `ava_runner`, which is the credential a
pure agent-runner dials with, and it stays invisible for the life of the cluster
because nothing re-runs the grant.

The standing `ALTER DEFAULT PRIVILEGES` in `ensure_runner_role` closes it going
forward (locked in `tests/shared/test_runner_role.py`), but only for tables
created after it was declared — no help to a cluster born before it shipped.
These pin the other half: the start path re-affirms the grants at the one moment
the schema is known to have changed.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

_RUNNER_PW = "runner-pw"  # a stub value the fake ensure_runner_role only echoes back


def test_start_refreshes_grants_between_the_schema_check_and_materialization() -> None:
    """The placement inside `ava start`, asserted on the real source.

    Brittle-looking on purpose, same argument as its sibling in
    `test_extension_materialize_ordering.py`: the invariant IS the order of
    calls, and nothing else in the process can observe it. The refresh has to
    follow the migration apply (there is nothing to re-affirm before it) and
    precede the extension materialization (which on a runner is the first reader
    that a missing grant breaks).
    """
    from cli.commands.start import _cmd_start_body

    src = inspect.getsource(_cmd_start_body)
    migrate_at = src.index("cmd_migrations_apply()")
    refresh_at = src.index("refresh_runner_grants_after_migration()")
    materialize_at = src.index("materialize_cluster_extensions()")

    assert migrate_at < refresh_at < materialize_at


def test_refresh_is_gated_on_a_migration_having_applied() -> None:
    """Not every start — only a start that changed the schema.

    `ensure_runner_role` opens a superuser connection and re-affirms ~15 grants.
    On the steady state there is nothing to re-affirm, so gating on the applied
    set keeps it off the hot path entirely; `cmd_migrations_apply` returns that
    set for exactly this reason.
    """
    from cli.commands.start import _cmd_start_body

    src = inspect.getsource(_cmd_start_body)
    guard = src[: src.index("refresh_runner_grants_after_migration()")]
    conditions = [line.strip() for line in guard.splitlines() if line.strip().startswith("if ")]
    assert "applied" in conditions[-1], (
        "the refresh must sit under a condition on `applied` — an ungated "
        f"re-affirm puts a superuser connection on every single start (found {conditions[-1]!r})"
    )


def _write_env(home: Path, **values: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")


@pytest.fixture
def _calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """Record `ensure_runner_role` calls instead of touching a real Postgres."""
    import shared.cluster as cl
    from cli.commands import _cluster_instance

    seen: list[tuple[str, str, str]] = []

    def _fake(identity: str, *, base_admin_url: str, runner_password: str) -> None:
        seen.append((identity, base_admin_url, runner_password))

    def _record(_home: Path) -> SimpleNamespace:
        return SimpleNamespace(ports={"postgres": 15432})

    def _admin_url(port: int) -> str:
        return f"postgresql://o@h:{port}/postgres"

    monkeypatch.setattr(cl, "ensure_runner_role", _fake)
    monkeypatch.setattr(cl, "get_record", _record)
    monkeypatch.setattr(_cluster_instance, "pg_admin_url", _admin_url)
    return seen


def test_refresh_re_affirms_the_identity_the_url_carries(
    unit_home: Path, _calls: list[tuple[str, str, str]]
) -> None:
    """Identity is READ from the URL, never re-derived — a cluster on its
    historical `ava_main` must not be re-affirmed as `ava`."""
    from cli.commands.ensure_db_role import refresh_runner_grants_after_migration

    _write_env(
        unit_home,
        AVA_DB_URL="postgresql://ava_main:pw@127.0.0.1:5433/ava_main",
        AVA_RUNNER_DB_PASSWORD=_RUNNER_PW,
    )
    refresh_runner_grants_after_migration()

    assert _calls == [("ava_main", "postgresql://o@h:15432/postgres", _RUNNER_PW)]


def test_refresh_skips_a_runner_home(unit_home: Path, _calls: list[tuple[str, str, str]]) -> None:
    """A pure agent-runner's `.env` carries no AVA_DB_URL — it fetches connection
    facts from the gateway — and it holds no admin credential. Touching roles
    from there is not a degraded case to warn about; it is not its job."""
    from cli.commands.ensure_db_role import refresh_runner_grants_after_migration

    _write_env(unit_home, AVA_GATEWAY_URL="http://gw:8000")
    refresh_runner_grants_after_migration()

    assert _calls == []


def test_refresh_does_not_adopt_the_role_on_a_pre_cutover_cluster(
    unit_home: Path, _calls: list[tuple[str, str, str]]
) -> None:
    """No `AVA_RUNNER_DB_PASSWORD` means this cluster never provisioned the
    runner role at all. `ensure_runner_role` would CREATE it and mint a
    credential — a real adoption, silently, on somebody's next start.
    `ava cluster ensure-db-role` is the deliberate door for that."""
    from cli.commands.ensure_db_role import refresh_runner_grants_after_migration

    _write_env(unit_home, AVA_DB_URL="postgresql://ava:pw@127.0.0.1:5433/ava")
    refresh_runner_grants_after_migration()

    assert _calls == []


def test_refresh_failure_names_the_operator_fix_and_does_not_abort(
    unit_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _calls: list[tuple[str, str, str]],
) -> None:
    """The data plane is already up by this point in the start sequence; a
    stale grant is recoverable and self-heals on the next migration. So this
    reports and continues — but it has to say what to run, because nothing else
    will surface it until an agent-runner hits `permission denied`."""
    import shared.cluster as cl
    from cli.commands.ensure_db_role import refresh_runner_grants_after_migration

    def _boom(identity: str, *, base_admin_url: str, runner_password: str) -> None:
        raise ConnectionError("could not connect to server")

    monkeypatch.setattr(cl, "ensure_runner_role", _boom)
    _write_env(
        unit_home,
        AVA_DB_URL="postgresql://ava:pw@127.0.0.1:5433/ava",
        AVA_RUNNER_DB_PASSWORD=_RUNNER_PW,
    )
    refresh_runner_grants_after_migration()

    err = capsys.readouterr().err
    assert "ava cluster ensure-db-role" in err
    assert "could not connect to server" in err

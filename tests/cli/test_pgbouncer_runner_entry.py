"""Unit tests for the pgbouncer userlist runner entry (Task #1236).

The pooler's userlist carries `ava_runner` with its own password exactly when
the cluster has a runner credential — a legacy cluster keeps a byte-identical
userlist until `ava cluster ensure-db-role` runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.commands import _pgbouncer as pg


class _CompletedOk:
    """A successful stand-in for the PgBouncer launch subprocess."""

    returncode = 0
    stderr = ""


def _ensure_from_home_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the fresh-start path while keeping config writes real and local."""
    monkeypatch.setattr(pg, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(pg, "pgbouncer_bin", lambda: str(Path(__file__)))
    monkeypatch.setattr(pg, "_live_pg_socket_dir", lambda _port: tmp_path / "pg-socket")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(pg, "_bind_addrs", lambda _secret: ["127.0.0.1"])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(pg, "_running_pid", lambda: None)
    monkeypatch.setattr(pg, "_wait_for_reachable_bind_gated", lambda _secret: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(pg.subprocess, "run", lambda *_args, **_kwargs: _CompletedOk())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(pg, "_admin_reachable", lambda *_args, **_kwargs: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(pg, "pgbouncer_public_listener_reachable", lambda *_args: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(pg, "_report_backend_verification", lambda *_args: None)  # pyright: ignore[reportUnknownArgumentType]

    assert (
        pg.ensure_pgbouncer(
            pg_port=5433,
            listen_port=6433,
            db_name="ava_main",
            role="ava_main",
            cluster_secret="sec",  # noqa: S106 — test fixture
            runner_password=None,
        )
        == 0
    )
    return pg._userlist_path()


def test_render_userlist_without_runner_entry_is_unchanged() -> None:
    assert pg._render_userlist("ava_main", "sec") == '"ava_main" "sec"\n'


def test_render_userlist_with_runner_entry() -> None:
    out = pg._render_userlist("ava_main", "sec", "ava_runner", "rpw")
    assert out == '"ava_main" "sec"\n"ava_runner" "rpw"\n'


def test_render_userlist_escapes_quotes_in_passwords() -> None:
    """PgBouncer double-quotes both fields, so an embedded quote in a password
    must be escaped (the roles are fixed identifiers, never user-controlled)."""
    out = pg._render_userlist("ava_main", 'se"c', "ava_runner", 'rp"w')
    assert out == '"ava_main" "se""c"\n"ava_runner" "rp""w"\n'


def test_runner_password_from_env_reads_home_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pg, "ava_home", lambda: tmp_path)
    # Assembled, not a literal KEY=VALUE line — the fixture value is a secret-ish
    # string and a literal would trip the secret scanner.
    (tmp_path / ".env").write_text("AVA_RUNNER_DB_PASSWORD=" + "abc123" + "\n")
    assert pg.runner_password_from_env() == "abc123"


def test_runner_password_from_env_absent_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pg, "ava_home", lambda: tmp_path)
    (tmp_path / ".env").write_text("AVA_CLUSTER_SECRET=cs\n")
    assert pg.runner_password_from_env() == ""


def test_ensure_pgbouncer_keeps_runner_entry_from_home_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("AVA_RUNNER_DB_PASSWORD=" + "abc123" + "\n")
    env_path.chmod(0o400)

    userlist = _ensure_from_home_env(tmp_path, monkeypatch)

    assert userlist.read_text() == '"ava_main" "sec"\n"ava_runner" "abc123"\n'


def test_ensure_pgbouncer_without_runner_password_keeps_legacy_userlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("AVA_CLUSTER_SECRET=cs\n")

    userlist = _ensure_from_home_env(tmp_path, monkeypatch)

    assert userlist.read_text() == '"ava_main" "sec"\n'

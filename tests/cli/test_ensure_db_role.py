"""End-to-end test of `ava cluster ensure-db-role` (Task #1236, renamed from
ensure-runner-role for issue #217).

Drives cmd_ensure_db_role against a REAL throwaway Postgres: the command
resolves the cluster's .env / registry / admin URL (monkeypatched to the
fixture), runs the same idempotent SQL as install birth, mints and persists
AVA_RUNNER_DB_PASSWORD, and the resulting credential actually works — the
runner can connect and read.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest
from dotenv import dotenv_values

from cli.commands import _cluster_instance as ci
from cli.commands import cmd_ensure_db_role
from shared import cluster as cl
from shared import paths
from shared.pg_tools import throwaway_postgres
from shared.url_secret import url_with_userinfo


class _FakeRec:
    def __init__(self, pg_port: int) -> None:
        self.ports = {"postgres": pg_port}


@pytest.fixture()
def runner_db() -> Generator[str, None, None]:
    with throwaway_postgres(schema_sql=_schema_sql()) as url:
        yield url


def _schema_sql() -> str:
    root = Path(__file__).resolve().parents[2]
    return (root / "db" / "schema.sql").read_text()


def _home_env(tmp_path: Path, url: str) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(
        f"AVA_DB_URL={url}\nAVA_CLUSTER_SECRET=cs\nAVA_MACHINE_SERVE_GATEWAY=true\n"
    )
    return home


def test_cmd_ensure_db_role_provisions_and_persists(
    runner_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = urlsplit(runner_db).port
    assert port is not None
    # The .env names the ava_citest identity (names-as-data), like a real gateway.
    db_url = f"postgresql://ava_citest:pw@127.0.0.1:{port}/ava_citest"
    home = _home_env(tmp_path, db_url)
    monkeypatch.setattr(paths, "ava_home", lambda: home)
    monkeypatch.setattr(cl, "get_record", lambda _h: _FakeRec(port))  # pyright: ignore[reportUnknownArgumentType]
    # The throwaway pg's admin socket is its own tmp dir, not the cluster socket
    # layout pg_admin_url probes — point the admin URL at the fixture instead.
    monkeypatch.setattr(ci, "pg_admin_url", lambda _p: db_url.rsplit("/", 1)[0] + "/postgres")  # pyright: ignore[reportUnknownArgumentType]

    assert cmd_ensure_db_role() == 0

    # The credential was minted + persisted into the gateway .env.
    env = dotenv_values(home / ".env")
    runner_pw = env.get("AVA_RUNNER_DB_PASSWORD")
    assert runner_pw, "AVA_RUNNER_DB_PASSWORD must be written to .env"
    runner_pw_str: str = runner_pw

    # And it genuinely works: connect as ava_runner with the persisted password.
    with psycopg.connect(
        url_with_userinfo(db_url, "ava_runner", runner_pw_str), autocommit=True
    ) as conn:
        conn.execute("SELECT * FROM agents")
    # The role is the least-privilege shape.
    with psycopg.connect(db_url.rsplit("/", 1)[0] + "/postgres", autocommit=True) as conn:
        row = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = 'ava_runner'").fetchone()
    assert row == (False,)


def test_cmd_ensure_db_role_keeps_existing_password(
    runner_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing AVA_RUNNER_DB_PASSWORD is kept (never rotated) and the role is
    re-affirmed against it — the re-run self-heal path."""
    port = urlsplit(runner_db).port
    assert port is not None
    db_url = f"postgresql://ava_citest:pw@127.0.0.1:{port}/ava_citest"
    home = _home_env(tmp_path, db_url)
    existing = "existing-runner-pw"
    with (home / ".env").open("a") as handle:
        handle.write(f"AVA_RUNNER_DB_PASSWORD={existing}\n")
    monkeypatch.setattr(paths, "ava_home", lambda: home)
    monkeypatch.setattr(cl, "get_record", lambda _h: _FakeRec(port))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(ci, "pg_admin_url", lambda _p: db_url.rsplit("/", 1)[0] + "/postgres")  # pyright: ignore[reportUnknownArgumentType]

    assert cmd_ensure_db_role() == 0
    assert dotenv_values(home / ".env")["AVA_RUNNER_DB_PASSWORD"] == existing
    existing_str: str = existing
    with psycopg.connect(
        url_with_userinfo(db_url, "ava_runner", existing_str), autocommit=True
    ) as conn:
        conn.execute("SELECT 1")


def test_cmd_ensure_db_role_refuses_home_without_db_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("AVA_MACHINE_SERVE_AGENT_RUNNER=true\n")
    monkeypatch.setattr(paths, "ava_home", lambda: home)
    assert cmd_ensure_db_role() == 1

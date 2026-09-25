"""Administrative and pooler connections use only the home's canonical socket."""

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from cli.commands import _pgbouncer as pooler
from shared import pg_admin


def test_missing_canonical_socket_does_not_discover_same_port_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    foreign = tmp_path / "ava-pg-foreign"
    foreign.mkdir()
    (foreign / ".s.PGSQL.5433").write_text("")
    monkeypatch.setattr(pg_admin, "pg_socket_dir", lambda: canonical)
    query = parse_qs(urlsplit(pg_admin.pg_admin_url(5433)).query)
    assert query == {"host": [str(canonical)], "port": ["5433"]}


def test_pgbouncer_ini_uses_only_canonical_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = tmp_path / "canonical"
    monkeypatch.setattr(pooler, "_pg_socket_dir", lambda: canonical)
    ini = pooler._render_ini(
        pg_port=5433,
        listen_port=6433,
        db_name="ava",
        role="ava",
        cluster_secret="",
    )
    assert f"host={canonical} port=5433" in ini

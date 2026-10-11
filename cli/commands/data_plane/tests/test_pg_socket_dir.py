"""Administrative and pooler connections use only the home's canonical socket."""

import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from base.cluster.machine import MachineIdentity
from base.db import pg_admin
from cli.commands.data_plane import cluster_instance as _ci
from cli.commands.data_plane import pgbouncer as pooler
from cli.tests.bootstrap.command_inputs import command_machine as command_machine


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    command_machine: MachineIdentity,
) -> None:
    canonical = tmp_path / "canonical"
    monkeypatch.setattr(pooler, "_pg_socket_dir", lambda: canonical)
    ini = pooler._render_ini(
        pg_port=5433,
        listen_port=6433,
        db_name="ava",
        machine=command_machine,
    )
    assert f"host={canonical} port=5433" in ini


def _pg_socket_path(root: Path, home: Path) -> Path:
    from base.cluster import home_slug

    return root / f"ava-pg-{home_slug(home)}"


def test_pg_socket_dir_rejects_symlink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The predictable /tmp socket location cannot follow a pre-placed symlink."""
    home = tmp_path / "home"
    monkeypatch.setattr(_ci, "ava_home", lambda: home)
    socket_dir = _pg_socket_path(tmp_path, home)
    socket_dir.symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlink"):
        _ci._pg_socket_dir(tmp_path)


def test_pg_socket_dir_rejects_foreign_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A socket directory owned by another local account is refused."""
    home = tmp_path / "home"
    monkeypatch.setattr(_ci, "ava_home", lambda: home)
    socket_dir = _pg_socket_path(tmp_path, home)
    socket_dir.mkdir()
    owner = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)

    with pytest.raises(RuntimeError, match="not owned by the current user"):
        _ci._pg_socket_dir(tmp_path)


def test_pg_socket_dir_repairs_mode_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Existing socket directories are tightened before Postgres binds a trust socket."""
    home = tmp_path / "home"
    monkeypatch.setattr(_ci, "ava_home", lambda: home)
    socket_dir = _pg_socket_path(tmp_path, home)
    socket_dir.mkdir()
    socket_dir.chmod(0o755)

    assert _ci._pg_socket_dir(tmp_path) == socket_dir
    assert socket_dir.stat().st_mode & 0o777 == 0o700

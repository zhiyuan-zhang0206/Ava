"""The start converge step accepts an installed PostgreSQL 17 only where the platform has no vendored artifact, and a remote-managed gateway needs no local server or extension."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from base.cluster.dataplane import pg_runtime, runtime_binaries
from base.config import settings
from cli.commands.converge._steps import _ensure_pg_binaries_step
from cli.commands.converge.spec import ConvergeCtx


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bindir = tmp_path / "pg17" / "bin"
    bindir.mkdir(parents=True)
    extension = bindir.parent / "share" / "extension"
    extension.mkdir(parents=True)
    library = bindir.parent / "lib"
    library.mkdir()
    (extension / "vector.control").write_text("default_version = '0.8.6'\n")
    (extension / "vector--0.8.6.sql").write_text("-- fixture\n")
    suffix = ".dylib" if sys.platform == "darwin" else ".so"
    (library / f"vector{suffix}").touch()
    body = (
        '#!/bin/sh\ncase "$1" in\n'
        ' --version) echo "PostgreSQL 17.11";;\n'
        f" --bindir) echo {shlex.quote(str(bindir))};;\n"
        f" --sharedir) echo {shlex.quote(str(extension.parent))};;\n"
        f" --pkglibdir) echo {shlex.quote(str(library))};;\n"
        " *) exit 3;;\nesac\n"
    )
    for name in ("postgres", "initdb", "pg_ctl", "pg_config", "pg_dump", "pg_restore"):
        tool = bindir / name
        tool.write_text(body)
        tool.chmod(0o700)
    monkeypatch.setattr(runtime_binaries, "vendored_pg_bin_dir", lambda: None)
    monkeypatch.setattr(runtime_binaries, "vendored_pg_supported", lambda: False)

    def installed_tool(name: str) -> Path:
        return bindir / name

    def no_path_tool(_name: str) -> None:
        return None

    monkeypatch.setattr(pg_runtime.get_backend(), "pg_binary_path", installed_tool)
    monkeypatch.setattr(pg_runtime.shutil, "which", no_path_tool)

    def forbid_download() -> Path:
        pytest.fail("An installed runtime must not trigger vendored downloads")

    monkeypatch.setattr(runtime_binaries, "ensure_pg_binaries", forbid_download)
    monkeypatch.setattr(runtime_binaries, "ensure_pgvector", forbid_download)
    return bindir


def test_start_converge_accepts_installed_pg17_where_no_artifact_exists(installed: Path) -> None:
    _ensure_pg_binaries_step(
        ConvergeCtx(installed.parent, installed.parent, frozenset({"gateway"}))
    )
    assert pg_runtime.pg_tool("postgres") == installed / "postgres"


def test_remote_managed_gateway_does_not_require_local_server_or_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def remote(_self: object) -> bool:
        return True

    def forbidden() -> None:
        pytest.fail("Remote-managed storage must not prepare a local server runtime")

    monkeypatch.setattr(type(settings.data_plane), "is_remote", property(remote))
    monkeypatch.setattr(pg_runtime, "ensure_pg_runtime", forbidden)
    _ensure_pg_binaries_step(ConvergeCtx(tmp_path, tmp_path, frozenset({"gateway"})))

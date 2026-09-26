"""Start must provision the toolchain that it will actually execute."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from cli.commands._converge_spec import ConvergeCtx
from cli.commands._converge_steps import _ensure_pg_binaries_step
from shared import pg_runtime, runtime_binaries
from shared.config import settings


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


def test_start_converge_accepts_installed_pg17_without_download(installed: Path) -> None:
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


@pytest.mark.parametrize("version", ["16.8", "18.0", "17.1beta1"])
def test_installed_wrong_major_or_unstable_version_fails_without_replacement(
    installed: Path, version: str
) -> None:
    (installed / "postgres").write_text(f'#!/bin/sh\necho "PostgreSQL {version}"\n')
    with pytest.raises(RuntimeError, match="PostgreSQL 17 is required"):
        pg_runtime.ensure_pg_runtime()


@pytest.mark.parametrize("missing", ["vector.control", "vector--0.8.6.sql", "library"])
def test_installed_missing_extension_fails_without_replacement(
    installed: Path, missing: str
) -> None:
    if missing == "library":
        next((installed.parent / "lib").iterdir()).unlink()
    else:
        (installed.parent / "share" / "extension" / missing).unlink()
    with pytest.raises(RuntimeError, match="Install pgvector"):
        pg_runtime.ensure_pg_runtime()


@pytest.mark.parametrize("script", ["vector--0.8.5.sql", "vector--0.8.5--0.8.6.sql"])
def test_pgvector_requires_install_script_matching_default_version(
    installed: Path, script: str
) -> None:
    extension = installed.parent / "share" / "extension"
    (extension / "vector--0.8.6.sql").rename(extension / script)
    with pytest.raises(RuntimeError, match="Install pgvector"):
        pg_runtime.ensure_pg_runtime()


@pytest.mark.parametrize("duplicate", ["'missing'", "'0.8.5'", "unquoted"])
def test_pgvector_rejects_multiple_default_assignments(installed: Path, duplicate: str) -> None:
    control = installed.parent / "share" / "extension" / "vector.control"
    control.write_text(control.read_text() + f"default_version = {duplicate}\n")
    with pytest.raises(RuntimeError, match="Install pgvector"):
        pg_runtime.ensure_pg_runtime()


def test_mixed_installations_fail_before_data_mutation(
    installed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "pg18"
    other.mkdir()
    (other / "pg_ctl").write_text((installed / "pg_ctl").read_text())
    (other / "pg_ctl").chmod(0o700)

    def mixed_tool(name: str) -> Path:
        return other / name if name == "pg_ctl" else installed / name

    monkeypatch.setattr(pg_runtime.get_backend(), "pg_binary_path", mixed_tool)
    with pytest.raises(RuntimeError, match="does not belong"):
        pg_runtime.ensure_pg_runtime()


@pytest.mark.parametrize("existing_vendor", [False, True])
def test_vendor_provisioning_has_one_explicit_selection_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing_vendor: bool
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        runtime_binaries, "vendored_pg_bin_dir", lambda: tmp_path if existing_vendor else None
    )
    monkeypatch.setattr(pg_runtime, "_installed_server", lambda: None)
    monkeypatch.setattr(runtime_binaries, "ensure_pg_binaries", lambda: calls.append("postgres"))
    monkeypatch.setattr(runtime_binaries, "ensure_pgvector", lambda: calls.append("pgvector"))
    pg_runtime.ensure_pg_runtime()
    assert calls == ["postgres", "pgvector"]


def test_existing_vendor_does_not_switch_to_installed(
    installed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        runtime_binaries, "vendored_pg_bin_dir", lambda: installed.parent / "vendor"
    )
    monkeypatch.setattr(runtime_binaries, "ensure_pg_binaries", lambda: calls.append("postgres"))
    monkeypatch.setattr(runtime_binaries, "ensure_pgvector", lambda: calls.append("pgvector"))
    pg_runtime.ensure_pg_runtime()
    assert calls == ["postgres", "pgvector"]

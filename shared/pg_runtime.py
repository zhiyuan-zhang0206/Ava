"""Provision the same PostgreSQL installation that runtime resolution selects."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from shared import runtime_binaries
from shared.pg_tools import pg_tool
from shared.platform_backend import get_backend


def _installed_server() -> Path | None:
    candidate = get_backend().pg_binary_path("postgres")
    if candidate is not None and candidate.exists():
        return candidate
    found = shutil.which("postgres")
    return Path(found) if found is not None else None


def _output(binary: Path, option: str) -> str:
    result = subprocess.run(  # noqa: S603 — resolved local PG executable, fixed inspection option
        [str(binary), option], capture_output=True, text=True, check=True, timeout=10
    )
    return result.stdout.strip()


def _require_pg17(binary: Path) -> None:
    version = _output(binary, "--version")
    if re.search(r"\bPostgreSQL\)?\s+17\.\d+(?=\s|$)", version) is None:
        raise RuntimeError(f"PostgreSQL 17 is required: {binary} reports {version!r}")


def _verify_installed_runtime(server: Path) -> None:
    """Reject mixed toolchains and missing extensions before any data mutation."""
    bindir = server.resolve().parent
    for name in ("postgres", "initdb", "pg_ctl", "pg_config", "pg_dump", "pg_restore"):
        tool = pg_tool(name)
        if tool.resolve().parent != bindir or not os.access(tool, os.X_OK):
            raise RuntimeError(f"PostgreSQL tool {tool} does not belong to {bindir}")
        _require_pg17(tool)
    config = bindir / "pg_config"
    if Path(_output(config, "--bindir")).resolve() != bindir:
        raise RuntimeError(f"PostgreSQL pg_config does not describe {bindir}")
    extension = Path(_output(config, "--sharedir")) / "extension"
    library = Path(_output(config, "--pkglibdir"))
    suffix = ".dylib" if sys.platform == "darwin" else ".so"
    if not (library / f"vector{suffix}").is_file() or not _has_vector_install_script(extension):
        raise RuntimeError(f"Install pgvector for the PostgreSQL 17 runtime at {bindir}")


def _has_vector_install_script(extension: Path) -> bool:
    """Packaged pgvector must carry the base script named by its control file."""
    control = extension / "vector.control"
    if not control.is_file():
        return False
    assignments = re.findall(r"^\s*default_version\s*=(.*)$", control.read_text(), re.MULTILINE)
    if len(assignments) != 1:
        return False
    version = re.fullmatch(r"\s*'([0-9]+(?:\.[0-9]+)+)'\s*(?:#.*)?", assignments[0])
    return version is not None and (extension / f"vector--{version[1]}.sql").is_file()


def ensure_pg_runtime() -> None:
    """Keep an existing vendor tree, or validate the installed PG17 toolchain.

    Download the pinned distribution only when no installation is selected.
    An incomplete installed runtime is an error, never a silent replacement.
    """
    if runtime_binaries.vendored_pg_bin_dir() is None:
        server = _installed_server()
        if server is not None:
            _verify_installed_runtime(server)
            return
    runtime_binaries.ensure_pg_binaries()
    runtime_binaries.ensure_pgvector()

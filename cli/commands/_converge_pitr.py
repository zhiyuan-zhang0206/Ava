"""Converge the disabled-by-default physical-backup filesystem foundation."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from cli.commands._converge_spec import ConvergeCtx
from shared.config import settings
from shared.private_storage import ensure_private_dir


def _atomic_publish(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    staged = Path(raw)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_stream:
            output.write(input_stream.read())
            output.flush()
            os.fsync(output.fileno())
        staged.chmod(0o700)
        staged.replace(destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        staged.unlink(missing_ok=True)


def converge_pitr_foundation(ctx: ConvergeCtx) -> None:
    """Publish the stable shim and private layout without changing PostgreSQL."""
    root = ensure_private_dir(ctx.ava_home / "physical-backup")
    ensure_private_dir(root / "spool")
    ensure_private_dir(root / "ack")
    runtime = ensure_private_dir(ctx.ava_home / "runtime" / "pg-archive")
    source = ctx.repo / "services" / "pitr" / "archive_shim.py"
    destination = runtime / "archive-shim"
    _atomic_publish(source, destination)
    result = subprocess.run(
        [str(destination), "--self-check"], check=False, capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        raise RuntimeError("stable PITR archive shim self-check failed")
    # Reading validates all cross-field invariants even while disabled. The key
    # file is deliberately not opened here: this PR publishes no remote writer.
    _ = settings.physical_backup

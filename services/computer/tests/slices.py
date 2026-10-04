"""The computer-use slice for tests: the daemon's builder over the live settings, with overrides."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from services.computer import mcp_daemon
from services.computer.config import ComputerUseConfig


def computer_use_config(**overrides: Any) -> ComputerUseConfig:
    return replace(mcp_daemon.computer_use_config(), **overrides)


def short_sock_dir() -> tuple[Path, Path, Any]:
    """A SHORT socket dir — AF_UNIX paths cap at ~104 bytes, and pytest's
    tmp_path (/private/var/folders/...) blows past it (OSError: path too long,
    which the guard treats as occupied). /tmp keeps the path short, same as
    the browser daemon tests. Returns (dir, socket path, cleanup)."""
    d = Path(tempfile.mkdtemp(prefix="ava-cmcp-", dir="/tmp"))
    return d, d / "computer-mcp.sock", shutil.rmtree

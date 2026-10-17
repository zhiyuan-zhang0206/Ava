from __future__ import annotations

import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from services.desktop.computer import mcp_daemon
from services.desktop.computer.config import ComputerUseConfig


def computer_use_config(**overrides: Any) -> ComputerUseConfig:
    return replace(mcp_daemon.computer_use_config(), **overrides)


def short_sock_dir() -> tuple[Path, Path, Any]:
    d = Path(tempfile.mkdtemp(prefix="meiva-cmcp-", dir="/tmp"))
    return d, d / "computer-mcp.sock", shutil.rmtree

from __future__ import annotations

import argparse
from pathlib import Path

from base.native_process.os_platform import file_lock
from base.paths.private_dir import ensure_private_dir
from cli import init_intent, start_intent


def prepare_init_identity(args: argparse.Namespace) -> Path:
    home = start_intent._home()
    ensure_private_dir(home)
    with file_lock(home / "start-intent.lock", timeout_s=30):
        init_intent.initialize_home(args, home)
    return home

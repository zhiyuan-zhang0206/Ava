"""Initialize a home's identity without starting it.

`prepare_init_identity` runs the identity half of `cli.init_intent.run_init` for a
source checkout: resolve the home, then publish its identity under the start-intent
lock. It starts nothing and raises instead of printing. Every step is looked up on
`cli.start_intent` at call time, so a test's monkeypatch of `_checkout` applies.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from base.host.private_storage import ensure_private_dir
from base.native_process.os_platform import file_lock
from cli import init_intent, start_intent


def prepare_init_identity(args: argparse.Namespace) -> Path:
    home = start_intent._home()
    ensure_private_dir(home)
    with file_lock(home / "start-intent.lock", timeout_s=30):
        init_intent.initialize_home(args, home)
    return home

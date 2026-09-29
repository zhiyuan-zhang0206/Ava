"""Prepare a home's start identity without starting it.

`prepare_start_identity` runs the identity half of `cli.start_intent.run_start`
for a source checkout: resolve and validate the home, then prepare its identity
under the start-intent lock. It starts nothing and raises instead of printing.
Every step is looked up on `cli.start_intent` at call time, so a test's
monkeypatch of `_checkout` or `_prepare_start_locked` applies.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from base.host.private_storage import ensure_private_dir
from base.native_process.os_platform import file_lock
from cli import start_intent
from cli.start_runtime import StartRuntime


def prepare_start_identity(args: argparse.Namespace) -> Path:
    home = start_intent._home(worktree=args.worktree)
    StartRuntime.development(start_intent._checkout()).validate(home)
    ensure_private_dir(home)
    with file_lock(home / "start-intent.lock", timeout_s=30):
        start_intent._prepare_start_locked(args, home)
    return home

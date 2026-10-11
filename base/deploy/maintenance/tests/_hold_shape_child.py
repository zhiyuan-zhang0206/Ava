"""A shepherd process of one command shape for the hold-driver tests; never imported by production.

Run as ``python _hold_shape_child.py <persistent|direct|relay|forked_relay>``. It mints drivers
through ``_hold_mint_child.py`` in the shape's way, prints each one, then stays alive until killed.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import time
from pathlib import Path

MINT = Path(__file__).with_name("_hold_mint_child.py")


def mint_and_print() -> None:
    proc = subprocess.run(  # noqa: S603 -- fixed argv, this interpreter runs the mint child
        [sys.executable, str(MINT)], capture_output=True, text=True, check=False
    )
    sys.stdout.write(proc.stdout.strip() + "\n")
    sys.stdout.flush()


def mint_through_shell_and_print(suffix: str) -> None:
    command = f"{sys.executable} {shlex.quote(str(MINT))}{suffix}"
    proc = subprocess.run(  # noqa: S602 -- the shell relay is the shape under test
        command, shell=True, capture_output=True, text=True, check=False
    )
    sys.stdout.write(proc.stdout.strip() + "\n")
    sys.stdout.flush()


shape = sys.argv[1]
if shape == "persistent":
    for _ in range(2):
        mint_and_print()
        time.sleep(0.2)
elif shape == "direct":
    mint_and_print()
elif shape == "relay":
    mint_through_shell_and_print("")
elif shape == "forked_relay":
    mint_through_shell_and_print(" ; :")
else:
    raise SystemExit(f"unknown shape: {shape}")
time.sleep(60)

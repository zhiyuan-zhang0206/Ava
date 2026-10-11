"""Holds a file lock in a separate interpreter; never imported by a production entry.

Run as ``python _file_lock_holder.py <repo-root> <lock-target> <ready-file> <hold-seconds>``.
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, sys.argv[1])
from base.native_process.os_platform import file_lock

with file_lock(pathlib.Path(sys.argv[2]), timeout_s=60):
    pathlib.Path(sys.argv[3]).write_text("1")
    time.sleep(float(sys.argv[4]))

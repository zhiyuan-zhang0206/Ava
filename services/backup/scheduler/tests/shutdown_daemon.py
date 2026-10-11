"""Disposable scheduler daemon entry for the real signal tests.

Usage: `shutdown_daemon.py <root> <mode> <postgres base>`.
"""

import sys
from pathlib import Path

from services.backup.scheduler.tests.test_scheduler_shutdown import _exercise_daemon

if __name__ == "__main__":
    _exercise_daemon(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]))

"""Separate interpreter that takes `backup_lock`, signals readiness and holds it.

Usage: `child_backup_lock_holder.py <ready file> <hold seconds>`.
"""

import sys
import time
from pathlib import Path

from services.backup.dump import backup_lock

if __name__ == "__main__":
    with backup_lock(timeout_s=60):
        Path(sys.argv[1]).write_text("1", encoding="utf-8")
        time.sleep(float(sys.argv[2]))

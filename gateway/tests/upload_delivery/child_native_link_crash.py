"""Child process that dies inside `private_storage`'s link step.

Usage: `child_native_link_crash.py <final path> <before-link|after-link>`. It exits 73
at the chosen point, leaving the temp file exactly as a hard death would.
"""

import os
import sys
from pathlib import Path

from base.host import private_storage

original = private_storage.os.link


def crash(source: str, target: str) -> None:
    if sys.argv[2] == "after-link":
        original(source, target)
    os._exit(73)


if __name__ == "__main__":
    private_storage.os.link = crash
    private_storage.create_private_bytes(Path(sys.argv[1]), b"bytes")

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

"""CI/online preparation adapter for the existing pinned collector downloader."""

from __future__ import annotations

import sys
from pathlib import Path

from shared.collector_artifact import (
    download_and_verify,
    platform_tag,
)


def main() -> None:
    destination = Path(sys.argv[1]).resolve()
    tag = platform_tag()
    if tag is None or tag.startswith("windows"):
        raise RuntimeError("collector release preparation currently supports POSIX only")
    destination.mkdir(mode=0o700)
    download_and_verify(tag, destination)


if __name__ == "__main__":
    main()

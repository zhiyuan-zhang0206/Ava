"""CI/online preparation adapter for the existing pinned collector downloader."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from cli.commands._otel_collector import (
    OTELCOL_CONTRIB_VERSION,
    _download_and_verify,
    platform_tag,
)
from shared.runtime_prepare import inventory_digest, tree_inventory


def main() -> None:
    destination = Path(sys.argv[1]).resolve()
    tag = platform_tag()
    if tag is None or tag.startswith("windows"):
        raise RuntimeError("collector release preparation currently supports POSIX only")
    destination.mkdir(mode=0o700)
    _download_and_verify(tag, destination)
    # This trusted build receipt remains beside, not inside, its own inventory.
    destination.with_suffix(".json").write_text(
        json.dumps(
            {
                "version": OTELCOL_CONTRIB_VERSION,
                "platform": tag,
                "digest": inventory_digest(tree_inventory(destination)),
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    main()

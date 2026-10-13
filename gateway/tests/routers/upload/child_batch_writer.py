"""Child process that writes an upload batch and hard-dies at a configured phase.

Reads one JSON object from stdin (`dsn`, `directory`, `phase`, `home`, `machine`,
optional `key`), points `AVA_HOME` at `home` before the gateway modules load, and
exits with status 41 at `phase` (`receiving`, `first-file`, `published`, `ready`).
"""

import json
import os
import sys
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool


def main() -> None:
    config = json.load(sys.stdin)
    os.environ["AVA_HOME"] = config["home"]
    os.environ["AVA_CONFIG_FETCH"] = "skip"
    from gateway.routers.upload import batches as owner

    directory = Path(config["directory"])
    original_publish = owner.publish_files
    original_link = os.link

    def link(source: str, target: str) -> None:
        original_link(source, target)
        if Path(target).parent == directory and config["phase"] == "first-file":
            os._exit(41)

    def publish(
        directory: Path, manifest: list[dict[str, object]], batch: list[owner.UploadItem]
    ) -> None:
        if config["phase"] == "receiving":
            os._exit(41)
        original_publish(directory, manifest, batch)
        if config["phase"] == "published":
            os._exit(41)

    os.link = link
    owner.publish_files = publish
    with ConnectionPool[psycopg.Connection](config["dsn"], min_size=1, max_size=1) as pool:
        owner.save_keyed_batch(
            pool,
            config.get("key", "crash"),
            123,
            directory,
            [("a.png", b"aaa", "image/png"), ("b.png", b"bbb", "image/png")],
            ["a.png", "b.png"],
            max_bytes=6,
            max_files=2,
            local_machine=config["machine"],
        )
    os._exit(41)


if __name__ == "__main__":
    main()

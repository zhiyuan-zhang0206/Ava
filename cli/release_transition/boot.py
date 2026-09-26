"""Pinned installed-image entry to the ordinary idempotent start lifecycle."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from cli.release_transition.request import ReleaseRef


def start_image(home: Path, registry: Path, release: ReleaseRef) -> int:
    """Load Settings only after explicit image, identity and home admission."""
    import platform

    from cli.parsers import build_parser
    from cli.start_intent import run_start
    from cli.start_runtime import admit_release

    os.environ["AVA_HOME"] = str(home)
    os.environ["AVA_CLUSTER_REGISTRY"] = str(registry)
    image = release.verify(home, platform.platform())
    runtime = admit_release(
        home, image, schema_digest=release.schema_digest, source_commit=release.source_commit
    )
    args = build_parser().parse_args(["start", "--persist-services"])
    return run_start(args, runtime=runtime)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    release = ReleaseRef(
        artifact_digest=args.artifact,
        manifest_digest=args.manifest,
        schema_digest=args.schema,
        source_commit=args.commit,
    )
    return start_image(args.home, args.registry, release)


if __name__ == "__main__":
    raise SystemExit(main())

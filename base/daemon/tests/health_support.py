"""Explicit health-test inputs: unknown entry images and bearer token digests."""

from pathlib import Path

from base.cluster.authority.api import token_digest as token_digest
from base.native_process.loaded_commit import LoadedCommit

__all__ = ["token_digest", "unknown_image"]


def unknown_image() -> LoadedCommit:
    """An honest non-Git entry fact for transport-only health consumers."""
    return LoadedCommit(Path(__file__).resolve().parents[3], None)

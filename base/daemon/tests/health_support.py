from pathlib import Path

from base.host.db_authority.api import token_digest as token_digest
from base.native_process.loaded_commit import LoadedCommit

__all__ = ["token_digest", "unknown_image"]


def unknown_image() -> LoadedCommit:
    return LoadedCommit(Path(__file__).resolve().parents[3], None)

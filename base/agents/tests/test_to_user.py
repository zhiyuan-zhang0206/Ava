"""File ownership and bounds of the shared context read."""

import os
from pathlib import Path
from typing import cast

import pytest

from base.agents.to_user import MAX_DOCUMENT_BYTES, ToUserFormat, read_document


def test_missing_empty_and_format_selection(tmp_path: Path) -> None:
    assert read_document(tmp_path, "html").content is None
    (tmp_path / "to_user.html").write_text("  ")
    assert read_document(tmp_path, "html").content is None
    (tmp_path / "to_user.md").write_text("# Ready")
    assert read_document(tmp_path, "markdown").content == "# Ready"
    assert read_document(tmp_path, "html").content is None
    with pytest.raises(ValueError, match="Unknown"):
        read_document(tmp_path, cast(ToUserFormat, "unknown"))


def test_atomic_replacement_changes_snapshot(tmp_path: Path) -> None:
    target = tmp_path / "to_user.html"
    target.write_text("<p>Working</p>")
    first = read_document(tmp_path, "html")
    replacement = tmp_path / ".next"
    replacement.write_text("<p>Ready</p>")
    replacement.replace(target)
    second = read_document(tmp_path, "html")
    assert second.content == "<p>Ready</p>"
    assert second.version != first.version
    assert second.modified_at is not None


def test_symlinks_devices_invalid_encoding_and_oversize_are_refused(tmp_path: Path) -> None:
    target = tmp_path / "to_user.html"
    secret = tmp_path / "private"
    secret.write_text("not a document")
    target.symlink_to(secret)
    with pytest.raises(OSError):
        read_document(tmp_path, "html")
    target.unlink()
    os.mkfifo(target)
    with pytest.raises(ValueError, match="regular file"):
        read_document(tmp_path, "html")
    target.unlink()
    target.write_bytes(b"\xff")
    with pytest.raises(UnicodeError):
        read_document(tmp_path, "html")
    target.write_bytes(b"x" * (MAX_DOCUMENT_BYTES + 1))
    with pytest.raises(ValueError, match="256 KiB"):
        read_document(tmp_path, "html")

"""Parent-side validation and deduplication of exec attachment registrations."""

from __future__ import annotations

from pathlib import Path

from agent.graph._attach_merge import merge_attachments
from agent.state import AttachEntry, AttachState
from shared.lm.attach import ATTACH_MAX_FILE_BYTES


def test_merge_preserves_order_and_last_label_wins(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    pending = AttachState(pending=[AttachEntry(path=str(first.resolve()), label="old")])

    merged = merge_attachments(
        pending,
        [
            {"path": str(second), "label": "second"},
            {"path": str(first), "label": "new"},
        ],
    )

    assert merged.pending == [
        AttachEntry(path=str(first.resolve()), label="new"),
        AttachEntry(path=str(second.resolve()), label="second"),
    ]


def test_merge_drops_invalid_or_tampered_entries(tmp_path: Path) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    directory = tmp_path / "directory"
    directory.mkdir()
    text = tmp_path / "notes.txt"
    text.write_text("text", encoding="utf-8")
    oversized = tmp_path / "large.png"
    with oversized.open("wb") as stream:
        stream.truncate(ATTACH_MAX_FILE_BYTES + 1)

    merged = merge_attachments(
        AttachState(),
        [
            {"path": str(image), "label": "ok"},
            {"path": str(tmp_path / "missing.png"), "label": None},
            {"path": str(directory), "label": None},
            {"path": str(text), "label": None},
            {"path": str(oversized), "label": None},
            {"path": str(image), "label": 1},
            {"label": None},
        ],
    )

    assert merged.pending == [AttachEntry(path=str(image.resolve()), label="ok")]


def test_merge_ignores_non_list_envelope_without_losing_pending(tmp_path: Path) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    pending = AttachState(pending=[AttachEntry(path=str(image.resolve()), label=None)])

    assert merge_attachments(pending, "tampered").pending == pending.pending  # type: ignore[arg-type]

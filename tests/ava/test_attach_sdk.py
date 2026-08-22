"""Contract tests for `ava.self.attach`'s child-local registration buffer."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ava._attach import attach, take_attachments
from shared.lm.attach import ATTACH_MAX_FILE_BYTES, ATTACH_MAX_LABEL_CHARS


@pytest.fixture(autouse=True)
def _clear_attachment_buffer() -> Iterator[None]:
    take_attachments()
    yield
    take_attachments()


def _exec_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AVA_EXEC_REQUEST_FILE", str(tmp_path / "request.json"))


def test_registers_resolved_path_and_drains_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _exec_child(monkeypatch, tmp_path)
    image = tmp_path / "result.png"
    image.write_bytes(b"png")

    attach(image, label="render result")

    assert take_attachments() == [{"path": str(image.resolve()), "label": "render result"}]
    assert take_attachments() == []


def test_rejects_calls_outside_exec_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AVA_EXEC_REQUEST_FILE", raising=False)
    image = tmp_path / "result.png"
    image.write_bytes(b"png")

    with pytest.raises(RuntimeError, match="only works inside an agent turn"):
        attach(image)


def test_validates_path_suffix_size_and_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _exec_child(monkeypatch, tmp_path)
    directory = tmp_path / "directory"
    directory.mkdir()
    text_file = tmp_path / "notes.txt"
    text_file.write_text("text", encoding="utf-8")
    oversized = tmp_path / "large.png"
    with oversized.open("wb") as stream:
        stream.truncate(ATTACH_MAX_FILE_BYTES + 1)
    image = tmp_path / "result.png"
    image.write_bytes(b"png")

    with pytest.raises(FileNotFoundError):
        attach(tmp_path / "missing.png")
    with pytest.raises(FileNotFoundError):
        attach(directory)
    with pytest.raises(ValueError, match=r"text files belong in exec output or ava.understand"):
        attach(text_file)
    with pytest.raises(ValueError, match="20 MiB"):
        attach(oversized)
    with pytest.raises(TypeError, match="label"):
        attach(image, label=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="120"):
        attach(image, label="x" * (ATTACH_MAX_LABEL_CHARS + 1))


def test_uses_workspace_relative_path_semantics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _exec_child(monkeypatch, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("ava.files._boot.agent_id", lambda: None)
    image = tmp_path / "relative.png"
    image.write_bytes(b"png")

    attach("relative.png")

    assert take_attachments() == [{"path": str(image.resolve()), "label": None}]


def test_take_attachments_drops_tampered_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ava import _attach

    _exec_child(monkeypatch, tmp_path)
    image = tmp_path / "result.png"
    image.write_bytes(b"png")
    attach(image)
    _attach._ATTACHMENTS.extend(
        [{"path": 1, "label": None}, {"path": "x", "label": 1}, object()]  # pyright: ignore[reportArgumentType]  # Deliberately tamper with the private buffer.
    )

    assert take_attachments() == [{"path": str(image.resolve()), "label": None}]
    assert take_attachments() == []

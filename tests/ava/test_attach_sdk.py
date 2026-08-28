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


@pytest.fixture(autouse=True)
def _media_capable_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The attach transport is a media-capable-model feature: a text-only
    model's attach call is rejected at the model gate (user ruling 2026-08-28),
    so the buffer contract tests run as a media-capable agent."""
    from shared.config import settings

    monkeypatch.setattr(settings.lm, "llm_model", "deepseek-v4-flash-vision-exp")


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


def test_rejects_text_only_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A text-only model's attach call fails at the model gate with a clear
    error and registers nothing — the member is hidden from its SDK docs, so
    the call is the only path that can reach it (user ruling 2026-08-28)."""
    from shared.config import settings

    _exec_child(monkeypatch, tmp_path)
    monkeypatch.setattr(settings.lm, "llm_model", "deepseek-v4-pro")
    image = tmp_path / "result.png"
    image.write_bytes(b"png")

    with pytest.raises(RuntimeError, match="text-only"):
        attach(image)

    assert take_attachments() == []


def test_rejects_modality_not_supported_by_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file whose modality the model's attach set excludes is rejected at
    registration with the allowed set in the error — never a silent pack-time
    skip (user ruling 2026-08-28)."""
    from shared.config import settings

    _exec_child(monkeypatch, tmp_path)
    # deepseek-v4-flash-vision-exp is image-only.
    monkeypatch.setattr(settings.lm, "llm_model", "deepseek-v4-flash-vision-exp")
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"mp4")

    with pytest.raises(ValueError, match=r"modality 'video'.*supported modalities: image"):
        attach(video)

    assert take_attachments() == []


def test_attach_modality_matrix_follows_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The per-model attach-modality matrix drives validation: Gemini accepts
    video (image/pdf/audio/video), Claude rejects it (image/pdf) — the
    registry's declared media matrix is the attach contract (user ruling
    2026-08-28)."""
    from shared.config import settings

    _exec_child(monkeypatch, tmp_path)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"mp4")
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")

    monkeypatch.setattr(settings.lm, "llm_model", "gemini-2.5-flash")
    attach(video)
    assert take_attachments() == [{"path": str(video.resolve()), "label": None}]
    attach(image)
    assert take_attachments() == [{"path": str(image.resolve()), "label": None}]

    monkeypatch.setattr(settings.lm, "llm_model", "claude-sonnet-4-6")
    with pytest.raises(ValueError, match=r"modality 'video'.*supported modalities: image, pdf"):
        attach(video)
    attach(image)
    assert take_attachments() == [{"path": str(image.resolve()), "label": None}]

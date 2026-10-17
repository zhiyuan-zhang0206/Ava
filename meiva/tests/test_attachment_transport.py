from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

import meiva
from base.lm.attach.constants import ATTACH_MAX_FILE_BYTES, ATTACH_MAX_LABEL_CHARS
from base.lm.catalog import ModelCatalog
from base.packages.tests.model_catalog import AddModels
from meiva import files
from meiva.sdk_surface import process_context
from meiva.sdk_surface.installation import Installation
from meiva.self import attach


def take_attachments() -> list[dict[str, object]]:
    update = meiva.state_update
    assert isinstance(update, dict)
    return cast("list[dict[str, object]]", update.pop("attach", []))


@pytest.fixture(autouse=True)
def _exec_turn() -> Iterator[None]:
    meiva.state = object()
    meiva.state_update = {}
    yield
    process_context.unbind_exec_turn()


@pytest.fixture(autouse=True)
def _media_capable_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from base.config import settings

    monkeypatch.setattr(settings.lm, "llm_model", "gpt-5.6-sol")


def test_registers_resolved_path_and_drains_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, model_installation: Installation
) -> None:
    monkeypatch.setattr(meiva, "__plugin_installation__", model_installation, raising=False)
    image = tmp_path / "result.png"
    image.write_bytes(b"png")

    attach(image, label="render result")

    assert take_attachments() == [{"path": str(image.resolve()), "label": "render result"}]
    assert take_attachments() == []


def test_rejects_calls_outside_exec_child(
    tmp_path: Path, *, model_installation: Installation, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(meiva, "__plugin_installation__", model_installation, raising=False)
    process_context.unbind_exec_turn()
    image = tmp_path / "result.png"
    image.write_bytes(b"png")

    with pytest.raises(RuntimeError, match="only works inside an agent turn"):
        attach(image)


def test_validates_path_suffix_size_and_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, model_installation: Installation
) -> None:
    monkeypatch.setattr(meiva, "__plugin_installation__", model_installation, raising=False)
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
    with pytest.raises(ValueError, match=r"text files belong in exec output or meiva.understand"):
        attach(text_file)
    with pytest.raises(ValueError, match="20 MiB"):
        attach(oversized)
    with pytest.raises(TypeError, match="label"):
        attach(image, label=cast(Any, 1))
    with pytest.raises(ValueError, match="120"):
        attach(image, label="x" * (ATTACH_MAX_LABEL_CHARS + 1))


def test_uses_workspace_relative_path_semantics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, model_installation: Installation
) -> None:
    monkeypatch.setattr(meiva, "__plugin_installation__", model_installation, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(files.agent_identity, "agent_id", lambda: None)
    image = tmp_path / "relative.png"
    image.write_bytes(b"png")

    attach("relative.png")

    assert take_attachments() == [{"path": str(image.resolve()), "label": None}]


def test_rejects_text_only_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, model_installation: Installation
) -> None:
    monkeypatch.setattr(meiva, "__plugin_installation__", model_installation, raising=False)
    from base.config import settings

    monkeypatch.setattr(settings.lm, "llm_model", "deepseek-v4-pro")
    image = tmp_path / "result.png"
    image.write_bytes(b"png")

    with pytest.raises(RuntimeError, match="text-only"):
        attach(image)

    assert take_attachments() == []


def test_rejects_model_withdrawn_to_its_text_only_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    add_models: AddModels,
    model_catalog: ModelCatalog,
    *,
    model_installation: Installation,
) -> None:
    monkeypatch.setattr(meiva, "__plugin_installation__", model_installation, raising=False)
    from dataclasses import replace

    from base.config import settings

    model = "glm-vision-fixture"
    model_catalog = add_models(
        model_catalog,
        {
            model: replace(
                model_catalog.models["glm-5.3"],
                spawnable=False,
                unavailable_fallback="glm-5.3",
                media_types=frozenset({"image"}),
            )
        },
    )
    monkeypatch.setattr(
        meiva, "__plugin_installation__", replace(model_installation, catalog=model_catalog)
    )
    monkeypatch.setattr(settings.lm, "llm_model", model)
    image = tmp_path / "result.png"
    image.write_bytes(b"png")

    with pytest.raises(RuntimeError, match="text-only"):
        attach(image)

    assert take_attachments() == []


def test_rejects_modality_not_supported_by_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, model_installation: Installation
) -> None:
    monkeypatch.setattr(meiva, "__plugin_installation__", model_installation, raising=False)
    from base.config import settings

    monkeypatch.setattr(settings.lm, "llm_model", "gpt-5.6-sol")
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"mp4")

    with pytest.raises(ValueError, match=r"modality 'video'.*supported modalities: image"):
        attach(video)

    assert take_attachments() == []


def test_attach_modality_matrix_follows_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, model_installation: Installation
) -> None:
    monkeypatch.setattr(meiva, "__plugin_installation__", model_installation, raising=False)
    from base.config import settings

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"mp4")
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")

    monkeypatch.setattr(settings.lm, "llm_model", "gemini-3.8-flash")
    attach(video)
    assert take_attachments() == [{"path": str(video.resolve()), "label": None}]
    attach(image)
    assert take_attachments() == [{"path": str(image.resolve()), "label": None}]

    monkeypatch.setattr(settings.lm, "llm_model", "claude-sonnet-5")
    with pytest.raises(ValueError, match=r"modality 'video'.*supported modalities: image, pdf"):
        attach(video)
    attach(image)
    assert take_attachments() == [{"path": str(image.resolve()), "label": None}]

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.exceptions import ModelRateLimitError

from ava.sdk_surface.batch import DEFAULT_BATCH_MAX_CONCURRENT
from ava.tests.fixtures.pin_agent import pin_no_identity
from ava.tests.fixtures.workspace import workspace as workspace
from ava.tests.namespaces.understand.provider_support import ProviderCapture, run_understand
from ava.tests.namespaces.understand.provider_support import fake_image as fake_image
from ava.tests.namespaces.understand.provider_support import fake_pdf as fake_pdf
from ava.tests.namespaces.understand.provider_support import fake_video as fake_video
from ava.tests.namespaces.understand.provider_support import mock_deepseek as mock_deepseek
from ava.tests.namespaces.understand.provider_support import mock_gemini as mock_gemini
from ava.tests.namespaces.understand.provider_support import understand_deps as understand_deps
from ava.understand import UnderstandError, understand
from ava.understand.answer import UnderstandDeps

pytestmark = pytest.mark.usefixtures("sdk_model_owner")


def test_text_mode_uses_deepseek(mock_deepseek: ProviderCapture) -> None:
    [out] = mock_deepseek.understand([{"prompt": "summarize", "text": "the quick brown fox"}])
    assert out == "fake answer"
    assert mock_deepseek.model == "deepseek-flash"
    assert mock_deepseek.content == [
        {"type": "text", "text": "the quick brown fox"},
        {"type": "text", "text": "summarize"},
    ]


def test_text_default_effort_is_max(mock_deepseek: ProviderCapture) -> None:
    mock_deepseek.understand([{"prompt": "x", "text": "some text"}])
    assert mock_deepseek.kwargs["reasoning_effort"] == "max"


def test_text_effort_flows_to_build_chat_model(mock_deepseek: ProviderCapture) -> None:
    mock_deepseek.understand([{"prompt": "x", "text": "some text"}], effort="low")
    assert mock_deepseek.kwargs["reasoning_effort"] == "low"


def test_text_effort_accepts_enum_member(mock_deepseek: ProviderCapture) -> None:
    from base.lm.effort import ReasoningEffort

    mock_deepseek.understand([{"prompt": "x", "text": "some text"}], effort=ReasoningEffort.XHIGH)
    assert mock_deepseek.kwargs["reasoning_effort"] == "xhigh"
    assert isinstance(mock_deepseek.kwargs["reasoning_effort"], str)


def test_effort_invalid_value_raises(mock_deepseek: ProviderCapture) -> None:
    with pytest.raises(ValueError, match="effort"):
        mock_deepseek.understand([{"prompt": "x", "text": "y"}], effort="ultra")
    with pytest.raises(ValueError, match="effort"):
        mock_deepseek.understand([{"prompt": "x", "text": "y"}], effort="minimal")
    with pytest.raises(TypeError, match="effort"):
        mock_deepseek.understand([{"prompt": "x", "text": "y"}], effort=cast(Any, 3))


def test_media_effort_preserves_supported_gemini_levels(
    mock_gemini: ProviderCapture, fake_image: Path
) -> None:
    for effort, level in [
        ("none", "low"),
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
    ]:
        mock_gemini.understand([{"prompt": "x", "paths": [str(fake_image)]}], effort=effort)
        assert mock_gemini.kwargs["media_thinking_level"] == level, f"effort={effort}"


def test_media_effort_rejects_unsupported_grade(
    mock_gemini: ProviderCapture, fake_image: Path
) -> None:
    with pytest.raises(ValueError, match="unsupported reasoning effort"):
        mock_gemini.understand([{"prompt": "x", "paths": [str(fake_image)]}], effort="xhigh")


def test_media_default_effort_max_keeps_settings_knob(
    mock_gemini: ProviderCapture, fake_image: Path
) -> None:
    mock_gemini.understand([{"prompt": "x", "paths": [str(fake_image)]}])
    assert mock_gemini.kwargs["media_thinking_level"] == mock_gemini.deps.media_thinking_level

    mock_gemini.configure(media_thinking_level="high")
    mock_gemini.understand([{"prompt": "x", "paths": [str(fake_image)]}], effort="max")
    assert mock_gemini.kwargs["media_thinking_level"] == "high"


def test_existing_text_file_uses_deepseek(mock_deepseek: ProviderCapture, tmp_path: Path) -> None:
    f = tmp_path / "notes.md"
    f.write_text("# Heading\nbody text", encoding="utf-8")
    mock_deepseek.understand([{"prompt": "what is the heading", "paths": [str(f)]}])
    assert mock_deepseek.model == "deepseek-flash"
    content = mock_deepseek.content
    assert content[0] == {"type": "text", "text": "# Heading\nbody text"}
    assert content[1] == {"type": "text", "text": "what is the heading"}


def test_path_not_found_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        understand([{"prompt": "x", "paths": [str(tmp_path / "nope.txt")]}])


def test_single_call_keywords_rejected(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("x")
    call_understand = cast(Any, understand)
    with pytest.raises(TypeError):
        call_understand(prompt="x", path=str(f))
    with pytest.raises(TypeError):
        call_understand(prompt="x", text="y")


def test_relative_path_resolves_to_workspace(
    mock_deepseek: ProviderCapture, workspace: Path
) -> None:
    workspace.mkdir(parents=True)
    (workspace / "notes.md").write_text("workspace material", encoding="utf-8")
    mock_deepseek.understand([{"prompt": "p", "paths": ["notes.md"]}])
    assert mock_deepseek.content[0] == {"type": "text", "text": "workspace material"}


def test_relative_path_resolves_to_home_before_identity(
    mock_deepseek: ProviderCapture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import patch

    pin_no_identity()
    (tmp_path / "h.txt").write_text("home material", encoding="utf-8")
    with patch.dict(os.environ, {"HOME": str(tmp_path)}):
        assert Path.home() == tmp_path
        mock_deepseek.understand([{"prompt": "p", "paths": ["h.txt"]}])
    assert mock_deepseek.content[0] == {"type": "text", "text": "home material"}


def test_text_list_content_response_is_flattened(mock_deepseek: ProviderCapture) -> None:
    mock_deepseek.respond_with(
        [
            {"type": "text", "text": "line one"},
            {"type": "thinking", "text": "ignored"},
            {"type": "text", "text": "line two"},
        ]
    )
    [out] = mock_deepseek.understand([{"prompt": "prompt", "text": "material"}])
    assert out == "line one\nline two"


def test_image_uses_gemini_flash_and_image_mime(
    mock_gemini: ProviderCapture, fake_image: Path
) -> None:
    [out] = mock_gemini.understand([{"prompt": "describe this", "paths": [str(fake_image)]}])
    assert out == "fake answer"
    assert mock_gemini.model == mock_gemini.deps.media_model
    content = mock_gemini.content
    assert content[0]["type"] == "media"
    assert content[0]["mime_type"] == "image/png"
    assert content[1] == {"type": "text", "text": "describe this"}


def test_video_uses_video_mime(mock_gemini: ProviderCapture, fake_video: Path) -> None:
    mock_gemini.understand([{"prompt": "summarize", "paths": [str(fake_video)]}])
    assert mock_gemini.model == mock_gemini.deps.media_model
    assert mock_gemini.content[0]["mime_type"] == "video/mp4"


def test_pdf_uses_pdf_mime(mock_gemini: ProviderCapture, fake_pdf: Path) -> None:
    mock_gemini.understand([{"prompt": "summarize", "paths": [str(fake_pdf)]}])
    assert mock_gemini.content[0]["mime_type"] == "application/pdf"


def test_media_default_knobs_high_res_medium_thinking(
    mock_gemini: ProviderCapture, fake_image: Path
) -> None:
    mock_gemini.understand([{"prompt": "x", "paths": [str(fake_image)]}])
    assert mock_gemini.kwargs["media_resolution"] == "high"
    assert mock_gemini.kwargs["media_thinking_level"] == "medium"


def test_text_model_config_flows_in(mock_deepseek: ProviderCapture) -> None:
    mock_deepseek.configure(text_model="claude-sonnet-5")
    mock_deepseek.understand([{"prompt": "x", "text": "some text"}])
    assert mock_deepseek.model == "claude-sonnet-5"


def test_media_model_config_flows_in(mock_gemini: ProviderCapture, fake_image: Path) -> None:
    mock_gemini.configure(media_model="gemini-3.1-pro-preview")
    mock_gemini.understand([{"prompt": "x", "paths": [str(fake_image)]}])
    assert mock_gemini.model == "gemini-3.1-pro-preview"


def test_media_resolution_config_flows_in(mock_gemini: ProviderCapture, fake_image: Path) -> None:
    mock_gemini.configure(media_resolution="low")
    mock_gemini.understand([{"prompt": "x", "paths": [str(fake_image)]}])
    assert mock_gemini.kwargs["media_resolution"] == "low"


def test_media_invalid_resolution_raises(mock_gemini: ProviderCapture, fake_image: Path) -> None:
    mock_gemini.configure(media_resolution="ultra")
    with pytest.raises(UnderstandError, match="AVA_UNDERSTAND_MEDIA_RESOLUTION"):
        mock_gemini.understand([{"prompt": "x", "paths": [str(fake_image)]}])


def test_media_base_url_config_flows_in(mock_gemini: ProviderCapture, fake_image: Path) -> None:
    mock_gemini.configure(media_base_url="http://localhost:8080/v1beta")
    mock_gemini.understand([{"prompt": "x", "paths": [str(fake_image)]}])
    assert mock_gemini.kwargs["base_url"] == "http://localhost:8080/v1beta"


def test_media_base_url_default_keeps_none(mock_gemini: ProviderCapture, fake_image: Path) -> None:
    mock_gemini.understand([{"prompt": "x", "paths": [str(fake_image)]}])
    assert mock_gemini.kwargs["base_url"] is None


def test_media_model_non_gemini_fails_fast(
    understand_deps: UnderstandDeps, fake_image: Path
) -> None:
    deps = dataclasses.replace(understand_deps, media_model="gpt-5.6-sol")
    with pytest.raises(UnderstandError, match="Gemini"):
        run_understand(deps, [{"prompt": "x", "paths": [str(fake_image)]}])


def test_media_wraps_missing_gemini_key(mock_gemini: ProviderCapture, fake_image: Path) -> None:
    def _raise(model: str, **kwargs: object):
        raise RuntimeError("GEMINI_API_KEY not set")

    mock_gemini.configure(build_chat_model=_raise)
    with pytest.raises(UnderstandError, match="GEMINI_API_KEY"):
        mock_gemini.understand([{"prompt": "x", "paths": [str(fake_image)]}])


def test_text_wraps_missing_deepseek_key(mock_deepseek: ProviderCapture) -> None:
    def _raise(model: str, **kwargs: object):
        raise RuntimeError("DEEPSEEK_API_KEY not set")

    mock_deepseek.configure(build_chat_model=_raise)
    with pytest.raises(UnderstandError, match="DEEPSEEK_API_KEY"):
        mock_deepseek.understand([{"prompt": "x", "text": "some text"}])


def test_raises_on_oversized_media(mock_gemini: ProviderCapture, tmp_path: Path) -> None:
    big = tmp_path / "huge.mp4"
    big.write_bytes(b"\x00" * (20 * 1024 * 1024 + 1))
    with pytest.raises(UnderstandError, match="exceeds"):
        mock_gemini.understand([{"prompt": "x", "paths": [str(big)]}])
    mock_gemini.invoke.assert_not_called()


def test_raises_on_undecodable_unknown_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    weird = tmp_path / "data.bin"
    weird.write_bytes(b"\xff\xfe\x00\x01\x80")
    with pytest.raises(UnderstandError, match="UTF-8"):
        understand([{"prompt": "x", "paths": [str(weird)]}])


def test_media_raises_on_empty_response(mock_gemini: ProviderCapture, fake_image: Path) -> None:
    mock_gemini.respond_with("")
    with pytest.raises(UnderstandError, match="empty response"):
        mock_gemini.understand([{"prompt": "x", "paths": [str(fake_image)]}])


def test_text_wraps_upstream_error(mock_deepseek: ProviderCapture) -> None:
    mock_deepseek.configure(retry=dataclasses.replace(mock_deepseek.deps.retry, attempts=0))
    error = ModelRateLimitError("rate limit")
    mock_deepseek.invoke.side_effect = error
    with pytest.raises(UnderstandError, match="rate limit") as raised:
        mock_deepseek.understand([{"prompt": "x", "text": "some text"}])
    assert raised.value.__cause__ is error
    mock_deepseek.invoke.assert_called_once()


@pytest.mark.parametrize(
    "error",
    [
        TypeError("invalid callback input"),
        ValueError("invalid response"),
        RuntimeError("rate limit"),
    ],
)
def test_unknown_model_error_preserves_identity_without_retry(
    mock_deepseek: ProviderCapture, error: Exception
) -> None:
    mock_deepseek.invoke.side_effect = error
    with pytest.raises(type(error)) as raised:
        mock_deepseek.understand([{"prompt": "x", "text": "some text"}])
    assert raised.value is error
    mock_deepseek.invoke.assert_called_once()


def test_paths_two_images_single_media_call(
    mock_gemini: ProviderCapture, fake_image: Path, tmp_path: Path
) -> None:
    img2 = tmp_path / "img2.png"
    img2.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x11" * 32)
    [out] = mock_gemini.understand(
        [{"prompt": "compare these", "paths": [str(fake_image), str(img2)]}]
    )
    assert out == "fake answer"
    assert mock_gemini.model == mock_gemini.deps.media_model
    assert mock_gemini.invoke.call_count == 1
    content = mock_gemini.content
    assert content[0] == {
        "type": "media",
        "data": fake_image.read_bytes(),
        "mime_type": "image/png",
    }
    assert content[1] == {"type": "media", "data": img2.read_bytes(), "mime_type": "image/png"}
    assert content[2] == {"type": "text", "text": "compare these"}


def test_paths_mixed_media_and_text_uses_media_model(
    mock_gemini: ProviderCapture, fake_image: Path, tmp_path: Path
) -> None:
    notes = tmp_path / "notes.md"
    notes.write_text("design notes", encoding="utf-8")
    [out] = mock_gemini.understand([{"prompt": "check", "paths": [str(fake_image), str(notes)]}])
    assert out == "fake answer"
    assert mock_gemini.model == mock_gemini.deps.media_model
    assert mock_gemini.invoke.call_count == 1
    content = mock_gemini.content
    assert content[0]["type"] == "media"
    assert content[1] == {"type": "text", "text": "design notes"}
    assert content[2] == {"type": "text", "text": "check"}


def test_paths_all_text_uses_text_model(mock_deepseek: ProviderCapture, tmp_path: Path) -> None:
    a = tmp_path / "a.md"
    a.write_text("material A", encoding="utf-8")
    b = tmp_path / "b.md"
    b.write_text("material B", encoding="utf-8")
    [out] = mock_deepseek.understand([{"prompt": "diff", "paths": [str(a), str(b)]}])
    assert out == "fake answer"
    assert mock_deepseek.model == "deepseek-flash"
    assert mock_deepseek.invoke.call_count == 1
    assert mock_deepseek.content == [
        {"type": "text", "text": "material A"},
        {"type": "text", "text": "material B"},
        {"type": "text", "text": "diff"},
    ]


def test_paths_validation() -> None:
    with pytest.raises(TypeError, match="must be a list of file paths"):
        understand([{"prompt": "p", "paths": "a.png"}])
    with pytest.raises(TypeError, match="must be a list of file paths"):
        understand(cast(Any, [{"prompt": "p", "paths": ("a.png", "b.png")}]))
    with pytest.raises(ValueError, match="must not be empty"):
        understand([{"prompt": "p", "paths": []}])
    with pytest.raises(TypeError, match="must be a path string or Path"):
        understand(cast(Any, [{"prompt": "p", "paths": [123]}]))
    with pytest.raises(ValueError, match="exactly one of 'text' / 'paths'"):
        understand([{"prompt": "p", "path": "a.png"}])
    with pytest.raises(ValueError, match="exactly one of 'text' / 'paths'"):
        understand([{"prompt": "p", "text": "x", "paths": ["b.png"]}])


def test_paths_missing_file_raises(mock_gemini: ProviderCapture, tmp_path: Path) -> None:
    good = tmp_path / "good.png"
    good.write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(FileNotFoundError, match="does not name an existing file"):
        mock_gemini.understand(
            [{"prompt": "p", "paths": [str(good), str(tmp_path / "missing.png")]}]
        )


def test_paths_oversized_media_raises(mock_gemini: ProviderCapture, tmp_path: Path) -> None:
    big = tmp_path / "huge.png"
    big.write_bytes(b"\x00" * (20 * 1024 * 1024 + 1))
    with pytest.raises(UnderstandError, match="exceeds"):
        mock_gemini.understand([{"prompt": "x", "paths": [str(big)]}])
    mock_gemini.invoke.assert_not_called()


def test_paths_undecodable_file_raises(tmp_path: Path) -> None:
    weird = tmp_path / "data.bin"
    weird.write_bytes(b"\xff\xfe\x00\x01\x80")
    with pytest.raises(UnderstandError, match="UTF-8"):
        understand([{"prompt": "x", "paths": [str(weird)]}])


def test_paths_auto_save_source_labels_list(
    mock_gemini: ProviderCapture, fake_image: Path, tmp_path: Path
) -> None:
    ws = tmp_path / "paths_ws"
    mock_gemini.configure(output_dir=lambda: ws / ".exec_output")
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    mock_gemini.understand([{"prompt": "compare", "paths": [str(fake_image), str(shot)]}])
    files = list((ws / ".exec_output").glob("understand_*.txt"))
    assert len(files) == 1
    assert f"# source: paths={[str(fake_image), str(shot)]!r}" in files[0].read_text()


def test_signature_is_targets_effort_max_concurrent() -> None:
    import inspect

    sig = inspect.signature(understand)
    assert list(sig.parameters) == ["targets", "effort", "max_concurrent"]
    assert sig.parameters["effort"].default == "max"
    assert sig.parameters["max_concurrent"].default == DEFAULT_BATCH_MAX_CONCURRENT

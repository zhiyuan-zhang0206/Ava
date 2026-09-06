"""Contract tests for turn-boundary attachment packing."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from PIL import Image

from shared.lm import provider_api
from shared.lm._plugin_providers import ensure_provider_plugins_loaded
from shared.lm.attach import (
    ATTACH_MAX_FILE_BYTES,
    ATTACH_MAX_FILES_PER_TURN,
    ATTACH_MAX_TOTAL_BYTES,
    AttachEntry,
    pack_attachments,
)
from shared.lm.factory import media_types_for_model
from shared.lm.provider_api import AttachPolicy, ProviderBinding


def _entry(path: Path, label: str | None = None) -> AttachEntry:
    return AttachEntry(path=str(path.resolve()), label=label)


def _png(path: Path, size: tuple[int, int] = (2, 2)) -> bytes:
    Image.new("RGB", size, "white").save(path)
    return path.read_bytes()


def _padded_png(path: Path, file_size: int, *, dimensions: tuple[int, int] = (2, 2)) -> None:
    image_bytes = _png(path, dimensions)
    assert len(image_bytes) <= file_size
    path.write_bytes(image_bytes + b"\0" * (file_size - len(image_bytes)))


def test_deepseek_image_uses_a_data_uri_block(tmp_path: Path) -> None:
    image_path = tmp_path / "example.png"
    image_bytes = _png(image_path)

    pack = pack_attachments("deepseek-v4-flash-vision-exp", [_entry(image_path)])

    assert pack is not None
    assert pack.delivered == [str(image_path.resolve())]
    assert pack.blocks[2] == {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{base64.b64encode(image_bytes).decode()}"},
    }


def test_blocks_interleave_each_caption_line_with_its_media(tmp_path: Path) -> None:
    # One delivered image, one skipped file: the content blocks must read
    # [text(notice), text(line1), image1, text(line2)] — every media block
    # immediately preceded by its own caption line (frontend pairing contract),
    # the skipped entry's line present without a media block.
    image_path = tmp_path / "example.png"
    image_bytes = _png(image_path)
    unknown = tmp_path / "notes.txt"
    unknown.write_text("not media")

    pack = pack_attachments(
        "gemini-3.8-flash", [_entry(image_path, "shot"), _entry(unknown, "notes")]
    )

    assert pack is not None
    assert [b.get("type") for b in pack.blocks] == ["text", "text", "image_url", "text"]
    line1 = pack.blocks[1]["text"]
    assert isinstance(line1, str) and line1.startswith("- [1] example.png (image/png,")
    assert isinstance(line1, str) and '— "shot"' in line1
    assert pack.blocks[2] == {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{base64.b64encode(image_bytes).decode()}"},
    }
    line3 = pack.blocks[3]["text"]
    assert isinstance(line3, str) and "not delivered: unknown media suffix" in line3


def test_gemini_video_uses_the_media_block_shape(tmp_path: Path) -> None:
    video_path = tmp_path / "clip.mp4"
    video_bytes = b"minimal-mp4"
    video_path.write_bytes(video_bytes)

    pack = pack_attachments("gemini-3.8-flash", [_entry(video_path)])

    assert pack is not None
    assert pack.blocks[2] == {"type": "media", "mime_type": "video/mp4", "data": video_bytes}


def test_claude_pdf_uses_an_anthropic_document_block(tmp_path: Path) -> None:
    pdf_path = tmp_path / "report.pdf"
    pdf_bytes = b"%PDF-1.7"
    pdf_path.write_bytes(pdf_bytes)

    pack = pack_attachments("claude-sonnet-5", [_entry(pdf_path)])

    assert pack is not None
    assert pack.blocks[2] == {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.b64encode(pdf_bytes).decode(),
        },
    }


def test_bad_files_are_skipped_without_aborting_the_pack(tmp_path: Path) -> None:
    missing = tmp_path / "missing.png"
    directory = tmp_path / "directory"
    directory.mkdir()
    unknown = tmp_path / "notes.txt"
    unknown.write_text("not media")

    pack = pack_attachments(
        "gemini-3.8-flash", [_entry(missing), _entry(directory), _entry(unknown)]
    )

    assert pack is not None
    # Skipped entries carry their caption line as a text block but never a
    # media block — the pack stays text-only.
    assert [b.get("type") for b in pack.blocks] == ["text", "text", "text", "text"]
    assert [reason for _, reason in pack.skipped] == [
        "file does not exist",
        "not a regular file",
        "unknown media suffix",
    ]


def test_uniform_and_provider_specific_size_caps_are_rechecked(tmp_path: Path) -> None:
    uniform = tmp_path / "uniform.mp4"
    uniform.write_bytes(b"x" * (ATTACH_MAX_FILE_BYTES + 1))
    claude_image = tmp_path / "claude.png"
    claude_image.write_bytes(b"x" * (10 * 1024 * 1024 + 1))

    gemini_pack = pack_attachments("gemini-3.8-flash", [_entry(uniform)])
    claude_pack = pack_attachments("claude-sonnet-5", [_entry(claude_image)])

    assert gemini_pack is not None
    assert claude_pack is not None
    assert gemini_pack.skipped[0][1] == "file exceeds 20 MiB limit"
    assert claude_pack.skipped[0][1] == "file exceeds 10 MiB limit"


def test_claude_attach_policy_size_limits(tmp_path: Path) -> None:
    oversized_image = tmp_path / "oversized.png"
    oversized_pdf = tmp_path / "oversized.pdf"
    delivered_image = tmp_path / "delivered.png"
    _padded_png(oversized_image, 11 * 1024 * 1024)
    oversized_pdf.write_bytes(b"x" * (33 * 1024 * 1024))
    _padded_png(delivered_image, 9 * 1024 * 1024)

    pack = pack_attachments(
        "claude-sonnet-5",
        [_entry(oversized_image), _entry(oversized_pdf), _entry(delivered_image)],
    )

    assert pack is not None
    assert pack.delivered == [str(delivered_image.resolve())]
    assert [reason for _, reason in pack.skipped] == [
        "file exceeds 10 MiB limit",
        "file exceeds 20 MiB limit",  # core ceiling beats the 32 MiB provider rule
    ]


def test_core_size_ceiling_precedes_provider_policy_limits(tmp_path: Path) -> None:
    oversized_image = tmp_path / "oversized.png"
    delivered_image = tmp_path / "delivered.png"
    _padded_png(oversized_image, 21 * 1024 * 1024)
    _padded_png(delivered_image, 9 * 1024 * 1024)

    pack = pack_attachments(
        "deepseek-v4-flash-vision-exp",
        [_entry(oversized_image), _entry(delivered_image)],
    )

    assert pack is not None
    assert pack.delivered == [str(delivered_image.resolve())]
    # The core 20 MiB ceiling is checked first; the deepseek binding's 32 MiB
    # rule cannot raise the effective cap (same precedence as before plugins).
    assert [reason for _, reason in pack.skipped] == ["file exceeds 20 MiB limit"]


def test_provider_without_attach_policy_uses_core_size_and_dimension_defaults(
    tmp_path: Path,
) -> None:
    oversized_image = tmp_path / "oversized.png"
    delivered_image = tmp_path / "delivered.png"
    _padded_png(oversized_image, ATTACH_MAX_FILE_BYTES + 1)
    _padded_png(delivered_image, ATTACH_MAX_FILE_BYTES, dimensions=(9000, 1))

    pack = pack_attachments(
        "gpt-5.6-sol",
        [_entry(oversized_image), _entry(delivered_image)],
    )

    assert pack is not None
    assert pack.delivered == [str(delivered_image.resolve())]
    assert [reason for _, reason in pack.skipped] == ["file exceeds 20 MiB limit"]


def test_deepseek_attach_policy_switches_dimension_tier_at_image_15(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.lm import attach

    monkeypatch.setattr(attach, "ATTACH_MAX_FILES_PER_TURN", 15)
    entries: list[AttachEntry] = []
    for index in range(1, 14):
        path = tmp_path / f"image-{index}.png"
        _png(path)
        entries.append(_entry(path))
    image_14 = tmp_path / "image-14.png"
    _png(image_14, (8001, 1))
    entries.append(_entry(image_14))
    image_15 = tmp_path / "image-15.png"
    _png(image_15, (4097, 1))
    entries.append(_entry(image_15))

    pack = pack_attachments("deepseek-v4-flash-vision-exp", entries)

    assert pack is not None
    assert str(image_14.resolve()) in pack.delivered
    assert pack.skipped == [(str(image_15.resolve()), "image exceeds 4096 px dimension limit")]


def test_builtin_provider_bindings_own_attach_policy() -> None:
    ensure_provider_plugins_loaded()

    assert provider_api.REGISTRY.bindings["claude-"].attach == AttachPolicy(
        file_size_limits={"image": 10 * 1024 * 1024, "pdf": 32 * 1024 * 1024},
        image_dimension_tiers=((1, 8000),),
        pdf_document_block=True,
    )
    assert provider_api.REGISTRY.bindings["deepseek-"].attach == AttachPolicy(
        file_size_limits={"image": 32 * 1024 * 1024},
        image_dimension_tiers=((1, 8192), (15, 4096)),
    )
    assert provider_api.REGISTRY.bindings["gpt-"].attach is None


def test_per_turn_file_count_and_total_byte_caps_keep_first_entries(tmp_path: Path) -> None:
    count_entries: list[AttachEntry] = []
    for index in range(ATTACH_MAX_FILES_PER_TURN + 1):
        path = tmp_path / f"clip-{index}.mp4"
        path.write_bytes(b"x")
        count_entries.append(_entry(path))

    count_pack = pack_attachments("gemini-3.8-flash", count_entries)

    assert count_pack is not None
    assert len(count_pack.delivered) == ATTACH_MAX_FILES_PER_TURN
    assert count_pack.skipped[-1][1] == "turn already has 8 delivered files"

    size_entries: list[AttachEntry] = []
    file_size = ATTACH_MAX_TOTAL_BYTES // 3 + 1  # below 20 MiB; 3 files exceed 48 MiB total
    for index in range(3):
        path = tmp_path / f"large-{index}.mp4"
        path.write_bytes(b"x" * file_size)
        size_entries.append(_entry(path))

    size_pack = pack_attachments("gemini-3.8-flash", size_entries)

    assert size_pack is not None
    assert len(size_pack.delivered) == 2
    assert size_pack.skipped[-1][1] == "would exceed 48 MiB total limit"


def test_image_dimensions_and_model_capabilities_are_enforced(tmp_path: Path) -> None:
    huge_image = tmp_path / "huge.png"
    Image.new("1", (9000, 9000)).save(huge_image)
    ordinary_image = tmp_path / "ordinary.png"
    _png(ordinary_image)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")

    deepseek_pack = pack_attachments("deepseek-v4-flash-vision-exp", [_entry(huge_image)])
    claude_pack = pack_attachments("claude-sonnet-5", [_entry(huge_image)])
    text_only_pack = pack_attachments("deepseek-v4-pro", [_entry(ordinary_image)])
    claude_video_pack = pack_attachments("claude-sonnet-5", [_entry(video)])

    assert deepseek_pack is not None
    assert claude_pack is not None
    assert text_only_pack is not None
    assert claude_video_pack is not None
    assert deepseek_pack.skipped[0][1] == "image exceeds 8192 px dimension limit"
    assert claude_pack.skipped[0][1] == "image exceeds 8000 px dimension limit"
    assert text_only_pack.skipped[0][1] == "your model cannot receive image"
    assert claude_video_pack.skipped[0][1] == "your model cannot receive video"


def test_non_claude_pdf_uses_generic_media_block(tmp_path: Path) -> None:
    pdf_path = tmp_path / "report.pdf"
    pdf_bytes = b"%PDF-1.7"
    pdf_path.write_bytes(pdf_bytes)

    pack = pack_attachments("gemini-3.8-flash", [_entry(pdf_path)])

    assert pack is not None
    assert pack.blocks[2] == {
        "type": "media",
        "mime_type": "application/pdf",
        "data": pdf_bytes,
    }


def test_caption_numbers_entries_and_sanitizes_labels(tmp_path: Path) -> None:
    image_path = tmp_path / "example.png"
    _png(image_path)
    unknown = tmp_path / "notes.txt"
    unknown.write_text("not media")

    pack = pack_attachments(
        "gemini-3.8-flash",
        [_entry(image_path, " screenshot\nfor\x00 review "), _entry(unknown, "notes")],
    )

    assert pack is not None
    assert "[1] example.png (image/png," in pack.text
    assert '— "screenshotfor review"' in pack.text
    assert "[2] notes.txt (unknown," in pack.text
    assert "not delivered: unknown media suffix" in pack.text
    assert "\nfor" not in pack.text
    assert "\x00" not in pack.text


def test_empty_and_all_skipped_entries_preserve_the_text_notice(tmp_path: Path) -> None:
    image_path = tmp_path / "example.png"
    _png(image_path)

    assert pack_attachments("gemini-3.8-flash", []) is None

    pack = pack_attachments("deepseek-v4-pro", [_entry(image_path)])

    assert pack is not None
    # The skipped entry's caption line is a text block; no media block.
    assert [b.get("type") for b in pack.blocks] == ["text", "text"]
    assert pack.delivered == []
    assert pack.skipped == [(str(image_path.resolve()), "your model cannot receive image")]
    assert "not delivered: your model cannot receive image" in pack.text


def test_media_types_use_registry_then_plugin_vision_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = ProviderBinding(
        prefix="attachment-plugin-",
        display_name="Attachment plugin",
        key_env="ATTACHMENT_PLUGIN_API_KEY",
        build=lambda _ctx: FakeListChatModel(responses=["unused"]),
        vision=True,
    )
    monkeypatch.setitem(provider_api.REGISTRY.bindings, binding.prefix, binding)

    assert media_types_for_model("gemini-3.8-flash") == frozenset(
        {"image", "pdf", "audio", "video"}
    )
    assert media_types_for_model("attachment-plugin-unregistered") == frozenset({"image"})
    assert media_types_for_model("kimi-unknown-x") == frozenset({"image"})
    assert media_types_for_model("deepseek-unknown-x") == frozenset()

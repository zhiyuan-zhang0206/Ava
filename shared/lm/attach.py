"""Pure packing of registered local files into provider-native media blocks.

``shared.lm.factory`` owns tiered media-capability resolution so packing and
message-endpoint image gating use the same core, plugin, and prefix answers.
"""

from __future__ import annotations

import base64
import stat
from dataclasses import dataclass, field
from pathlib import Path

from shared.lm import provider_api
from shared.lm._plugin_providers import ensure_provider_plugins_loaded
from shared.lm.factory import attach_modalities_for_model
from shared.lm.provider_api import AttachPolicy

# Moved from ava._understand so both paths classify binary media identically.
ATTACH_MEDIA_MIME: dict[str, str] = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".m4v": "video/x-m4v",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".pdf": "application/pdf",
}

ATTACH_MAX_FILE_BYTES = 20 * 1024 * 1024
ATTACH_MAX_FILES_PER_TURN = 8
ATTACH_MAX_TOTAL_BYTES = 48 * 1024 * 1024
ATTACH_MAX_LABEL_CHARS = 120

# Keep the notice bare: attachments persist in the message history, so any
# extra instruction is redundant copy (user ruling 2026-08-30).
_ATTACH_NOTICE = "[system] Files attached during this turn:"


@dataclass(frozen=True)
class AttachEntry:
    """One attachment whose path was resolved when it was registered."""

    path: str
    label: str | None


@dataclass(frozen=True)
class AttachmentPack:
    """Caption plus native content blocks ready for one HumanMessage."""

    blocks: list[dict[str, object]]
    text: str
    delivered: list[str]
    skipped: list[tuple[str, str]]


@dataclass(frozen=True)
class _AttachmentFile:
    """A regular recognized file after its pack-time stat check."""

    path: Path
    path_text: str
    size: int
    mime: str
    media_type: str


@dataclass(frozen=True)
class _SkippedAttachment:
    """A file that cannot reach the model, retained for the text caption."""

    path: str
    mime: str | None
    size: int | None
    reason: str


def _new_media_blocks() -> list[dict[str, object]]:
    return []


def _new_delivered_paths() -> list[str]:
    return []


def _new_skipped_paths() -> list[tuple[str, str]]:
    return []


def _new_delivered_flags() -> list[bool]:
    return []


@dataclass
class _PackingState:
    """The ordered pack output while files are accepted or skipped.

    ``line_blocks`` / ``delivered_flags`` run in ENTRY order (one text block and
    one flag per entry, delivered or skipped) so the final content blocks can
    interleave each entry's caption line with its media block — the model sees
    every file's label right beside its image, and the frontend timeline can
    pair each delivered image with its own caption line structurally.
    """

    media_blocks: list[dict[str, object]] = field(default_factory=_new_media_blocks)
    caption_lines: list[str] = field(default_factory=lambda: [_ATTACH_NOTICE])
    line_blocks: list[dict[str, object]] = field(default_factory=_new_media_blocks)
    delivered_flags: list[bool] = field(default_factory=_new_delivered_flags)
    delivered: list[str] = field(default_factory=_new_delivered_paths)
    skipped: list[tuple[str, str]] = field(default_factory=_new_skipped_paths)
    delivered_bytes: int = 0
    delivered_images: int = 0

    def skip(self, index: int, label: str | None, attachment: _SkippedAttachment) -> None:
        self.skipped.append((attachment.path, attachment.reason))
        line = _caption_line(
            index,
            attachment.path,
            attachment.mime,
            attachment.size,
            label,
            attachment.reason,
        )
        self.caption_lines.append(line)
        self.line_blocks.append({"type": "text", "text": line})
        self.delivered_flags.append(False)

    def deliver(
        self,
        model: str,
        index: int,
        label: str | None,
        attachment: _AttachmentFile,
        data: bytes,
    ) -> None:
        self.media_blocks.append(
            _content_block(model, attachment.media_type, attachment.mime, data)
        )
        self.delivered.append(attachment.path_text)
        self.delivered_bytes += len(data)
        if attachment.media_type == "image":
            self.delivered_images += 1
        line = _caption_line(index, attachment.path_text, attachment.mime, len(data), label, None)
        self.caption_lines.append(line)
        self.line_blocks.append({"type": "text", "text": line})
        self.delivered_flags.append(True)


def pack_attachments(model: str, entries: list[AttachEntry]) -> AttachmentPack | None:
    """Pack current attachment files into content blocks without raising on bad files."""
    if not entries:
        return None

    # The attach contract, not the raw endpoint matrix: a model with an
    # attach-specific `attach_modalities` declaration packs exactly that set,
    # so a file attach() already rejected never slips through as a caption.
    supported_media_types = attach_modalities_for_model(model)
    state = _PackingState()

    for index, entry in enumerate(entries, start=1):
        attachment = _attachment_file(entry)
        if isinstance(attachment, _SkippedAttachment):
            state.skip(index, entry.label, attachment)
            continue
        if attachment.media_type not in supported_media_types:
            state.skip(
                index,
                entry.label,
                _skip_for(attachment, f"your model cannot receive {attachment.media_type}"),
            )
            continue
        if reason := _delivery_error(model, state, attachment.size, attachment.media_type):
            state.skip(index, entry.label, _skip_for(attachment, reason))
            continue
        if reason := _image_dimension_error(model, state.delivered_images, attachment):
            state.skip(index, entry.label, _skip_for(attachment, reason))
            continue
        data, read_error = _read_attachment(attachment)
        if read_error is not None:
            state.skip(index, entry.label, _skip_for(attachment, read_error))
            continue
        assert data is not None  # noqa: S101 — paired result from _read_attachment
        if reason := _delivery_error(model, state, len(data), attachment.media_type):
            state.skip(index, entry.label, _skip_for(attachment, reason, len(data)))
            continue
        state.deliver(model, index, entry.label, attachment, data)

    text = "\n".join(state.caption_lines)
    # Interleave each entry's caption line with its media block, ahead of the
    # leading notice: [text(notice), text(line1), media1, text(line2), media2, ...].
    # Every delivered media block is immediately preceded by its own caption
    # text block, so consumers can pair a file's label with its image without
    # re-parsing the joined caption (shared/timeline._attach_image_captions).
    blocks: list[dict[str, object]] = [{"type": "text", "text": state.caption_lines[0]}]
    media_index = 0
    for line_block, delivered_flag in zip(state.line_blocks, state.delivered_flags, strict=True):
        blocks.append(line_block)
        if delivered_flag:
            blocks.append(state.media_blocks[media_index])
            media_index += 1
    return AttachmentPack(
        blocks=blocks,
        text=text,
        delivered=state.delivered,
        skipped=state.skipped,
    )


def _attachment_file(entry: AttachEntry) -> _AttachmentFile | _SkippedAttachment:
    try:
        path = Path(entry.path).resolve()
    except (OSError, ValueError):
        return _SkippedAttachment(entry.path, None, None, "invalid file path")
    try:
        file_stat = path.stat()
    except FileNotFoundError:
        return _SkippedAttachment(str(path), None, None, "file does not exist")
    except OSError:
        return _SkippedAttachment(str(path), None, None, "cannot stat file")
    if not stat.S_ISREG(file_stat.st_mode):
        return _SkippedAttachment(str(path), None, file_stat.st_size, "not a regular file")
    mime = ATTACH_MEDIA_MIME.get(path.suffix.lower())
    if mime is None:
        return _SkippedAttachment(str(path), None, file_stat.st_size, "unknown media suffix")
    return _AttachmentFile(
        path=path,
        path_text=str(path),
        size=file_stat.st_size,
        mime=mime,
        media_type=_media_type_for_mime(mime),
    )


def _skip_for(
    attachment: _AttachmentFile, reason: str, size: int | None = None
) -> _SkippedAttachment:
    attachment_size = attachment.size if size is None else size
    return _SkippedAttachment(attachment.path_text, attachment.mime, attachment_size, reason)


def _delivery_error(model: str, state: _PackingState, size: int, media_type: str) -> str | None:
    # The core ceiling is checked before the provider policy so a policy can
    # only narrow the effective cap, never raise it (pre-plugin behavior).
    if size > ATTACH_MAX_FILE_BYTES:
        return f"file exceeds {_mib(ATTACH_MAX_FILE_BYTES)} limit"
    provider_size_limit = _file_size_limit(model, media_type)
    if size > provider_size_limit:
        return f"file exceeds {_mib(provider_size_limit)} limit"
    if len(state.delivered) >= ATTACH_MAX_FILES_PER_TURN:
        return f"turn already has {ATTACH_MAX_FILES_PER_TURN} delivered files"
    if state.delivered_bytes + size > ATTACH_MAX_TOTAL_BYTES:
        return f"would exceed {_mib(ATTACH_MAX_TOTAL_BYTES)} total limit"
    return None


def _image_dimension_error(
    model: str, delivered_images: int, attachment: _AttachmentFile
) -> str | None:
    if attachment.media_type != "image":
        return None
    dimensions, read_error = _image_dimensions(attachment.path)
    if read_error is not None:
        return read_error
    assert dimensions is not None  # noqa: S101 — paired result from _image_dimensions
    dimension_limit = _image_dimension_limit(model, delivered_images + 1)
    if max(dimensions) > dimension_limit:
        return f"image exceeds {dimension_limit} px dimension limit"
    return None


def _read_attachment(attachment: _AttachmentFile) -> tuple[bytes | None, str | None]:
    try:
        return attachment.path.read_bytes(), None
    except OSError:
        return None, "cannot read file"


def _media_type_for_mime(mime: str) -> str:
    if mime == "application/pdf":
        return "pdf"
    return mime.split("/", maxsplit=1)[0]


def _file_size_limit(model: str, media_type: str) -> int:
    policy = _attach_policy(model)
    if policy is not None:
        return policy.file_size_limits.get(media_type, ATTACH_MAX_FILE_BYTES)
    return ATTACH_MAX_FILE_BYTES


def _attach_policy(model: str) -> AttachPolicy | None:
    ensure_provider_plugins_loaded()
    for prefix, binding in provider_api.REGISTRY.bindings.items():
        if model.startswith(prefix):
            return binding.attach
    return None


def _image_dimensions(path: Path) -> tuple[tuple[int, int] | None, str | None]:
    """Read only image metadata; bad image bytes become a captioned skip."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as image:
            return image.size, None
    except (Image.DecompressionBombError, OSError, UnidentifiedImageError, ValueError):
        return None, "cannot read image dimensions"


def _image_dimension_limit(model: str, image_count: int) -> int:
    dimension_limit = 2**31 - 1
    policy = _attach_policy(model)
    if policy is None:
        return dimension_limit
    for min_image_count, max_px in policy.image_dimension_tiers:
        if min_image_count > image_count:
            break
        dimension_limit = max_px
    return dimension_limit


def _content_block(model: str, media_type: str, mime: str, file_bytes: bytes) -> dict[str, object]:
    if media_type == "image":
        encoded = base64.b64encode(file_bytes).decode("ascii")
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
    policy = _attach_policy(model)
    if media_type == "pdf" and policy is not None and policy.pdf_document_block:
        encoded = base64.b64encode(file_bytes).decode("ascii")
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": encoded},
        }
    return {"type": "media", "mime_type": mime, "data": file_bytes}


def _caption_line(
    index: int,
    path: str,
    mime: str | None,
    size: int | None,
    label: str | None,
    reason: str | None,
) -> str:
    details = mime or "unknown"
    if size is not None:
        details = f"{details}, {_format_size(size)}"
    caption = f"- [{index}] {Path(path).name} ({details})"
    sanitized_label = _sanitize_label(label)
    if sanitized_label is not None:
        caption += f' — "{sanitized_label}"'
    if reason is not None:
        caption += f" — not delivered: {reason}"
    return caption


def _sanitize_label(label: str | None) -> str | None:
    if label is None:
        return None
    sanitized = "".join(char for char in label if char >= " " and char != "\x7f").strip()
    return sanitized[:ATTACH_MAX_LABEL_CHARS] or None


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    return f"{size / 1024:.1f} KiB"


def _mib(size: int) -> str:
    return f"{size // (1024 * 1024)} MiB"

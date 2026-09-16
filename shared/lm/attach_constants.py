"""Attachment limits and the shared suffix-to-MIME table — a pure-data leaf.

The constants below are read by paths that must not pull the provider stack:
the SDK registration/validation surface (`ava._attach`), the SDK media
classification (`ava.understand`), the exec merge validator
(`agent.graph._attach_merge`), and the turn-boundary packer
(`shared.lm.attach`) itself. Keep this module import-free; anything that needs
LangChain belongs one level up in `shared.lm.attach` (startup-laziness
invariant, task #3587).
"""

from __future__ import annotations

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

# Attachment payload guards (task #3696 exception inventory): per-file ceiling
# (one file cannot dominate the turn), per-turn count and byte total (a turn's
# media stays bounded before packing), label cap (rides the merged prompt).
# Protective bounds, not cluster tuning knobs.
ATTACH_MAX_FILE_BYTES = 20 * 1024 * 1024
ATTACH_MAX_FILES_PER_TURN = 8
ATTACH_MAX_TOTAL_BYTES = 48 * 1024 * 1024
ATTACH_MAX_LABEL_CHARS = 120

"""Register media for delivery across the child-to-parent attachment transport.

The child-local buffer is drained into the exec envelope, checkpointed by the
parent, and turned into one media message appended right after the exec output
of the registering turn (user ruling 2026-08-26).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ava._sdk_validation import coerce_str
from ava.files import _resolve
from shared.lm.attach import (
    ATTACH_MAX_FILE_BYTES,
    ATTACH_MAX_LABEL_CHARS,
    ATTACH_MEDIA_MIME,
)
from shared.log import logger

_ATTACHMENTS: list[dict[str, Any]] = []


def attach_available() -> bool:
    """Whether the current agent's model can receive media attachments.

    The single gate behind the text-only ruling (2026-08-28): `attach()`
    raises for a model with no attachable modality, and the system-prompt /
    help surfaces hide the member entirely. See
    `shared.lm.factory.attach_modalities_for_model` for the resolution."""
    return _attach_unavailable_reason() is None


def media_gated_members() -> frozenset[str]:
    """Dotted ``ava`` member paths unavailable for the current model's media
    capability — ``ava.self.attach`` on a text-only model (user ruling
    2026-08-28). The help() renderer hides these from the SDK docs; empty for
    a media-capable model."""
    if attach_available():
        return frozenset()
    return frozenset({"ava.self.attach"})


def _attach_unavailable_reason() -> str | None:
    """Why ``attach`` is unavailable for the current agent's model, or None.

    A model with an empty attach-modality set (text-only, or an explicit empty
    ``attach_modalities`` declaration) cannot receive any attached media, so
    registering files for its next turn is a contradiction — the SDK docs drop
    the member and the call fails with this reason (user ruling 2026-08-28)."""
    from shared.config.turn_view import turn_settings
    from shared.lm.factory import attach_modalities_for_model

    model = turn_settings.lm.llm_model
    if attach_modalities_for_model(model):
        return None
    return f"your model ({model}) is text-only and cannot receive media attachments"


def _validate_modality(suffix: str) -> None:
    """Reject a file whose modality the current model's attach set does not
    include — a clear error at registration, never a silent pack-time skip
    (user ruling 2026-08-28)."""
    from shared.config.turn_view import turn_settings
    from shared.lm.factory import attach_modalities_for_model

    model = turn_settings.lm.llm_model
    mime = ATTACH_MEDIA_MIME[suffix]
    modality = "pdf" if mime == "application/pdf" else mime.split("/", maxsplit=1)[0]
    allowed = attach_modalities_for_model(model)
    if modality in allowed:
        return
    raise ValueError(
        f"attachment modality {modality!r} is not supported by your model ({model}); "
        f"supported modalities: {', '.join(sorted(allowed)) or 'none'}"
    )


def attach(path: str | Path, *, label: str | None = None) -> None:
    """Register a local media file for your next turn.

    Use this when your next response needs to inspect a file generated during
    this turn. The file is read when this turn ends, so keep it available until
    then. Your model receives supported media natively; a file whose modality
    your model cannot receive (e.g. video on an image-only model) is rejected
    with an error naming the allowed set. Attach at most 8 files and 48 MiB per
    turn, with a 20 MiB limit per file. Raises an error outside an agent turn.
    """
    if reason := _attach_unavailable_reason():
        raise RuntimeError(
            f"ava.self.attach is unavailable: {reason}; switch to a vision-capable model"
        )
    path = coerce_str(path, "path", allow_types=(Path,))
    label = coerce_str(label, "label", allow_none=True)
    if not os.environ.get("AVA_EXEC_REQUEST_FILE"):
        raise RuntimeError(
            "ava.self.attach only works inside an agent turn (execute_code); "
            "outside a turn there is no runner to deliver the attachment"
        )
    resolved = _resolve(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"path {str(path)!r} does not name an existing file ({resolved})")
    suffix = resolved.suffix.lower()
    if suffix not in ATTACH_MEDIA_MIME:
        supported = ", ".join(sorted(ATTACH_MEDIA_MIME))
        raise ValueError(
            f"unsupported attachment suffix {resolved.suffix!r}; supported suffixes: {supported}; "
            "text files belong in exec output or ava.understand"
        )
    _validate_modality(suffix)
    if resolved.stat().st_size > ATTACH_MAX_FILE_BYTES:
        raise ValueError(f"attachment exceeds the 20 MiB per-file limit: {resolved}")
    if not isinstance(label, str | None):
        raise TypeError("attachment label must be a str or None")
    if label is not None and len(label) > ATTACH_MAX_LABEL_CHARS:
        raise ValueError(f"attachment label exceeds {ATTACH_MAX_LABEL_CHARS} characters")
    _ATTACHMENTS.append({"path": str(resolved), "label": label})


def take_attachments() -> list[dict[str, Any]]:
    """Drain registered attachments for the exec child result envelope."""
    entries = list(_ATTACHMENTS)
    _ATTACHMENTS.clear()
    accepted: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            logger.warning("dropping tampered attachment registration with non-dict entry")
            continue
        path = entry.get("path")
        label = entry.get("label")
        if not isinstance(path, str) or not isinstance(label, str | None):
            logger.warning("dropping tampered attachment registration with invalid shape")
            continue
        accepted.append({"path": path, "label": label})
    return accepted

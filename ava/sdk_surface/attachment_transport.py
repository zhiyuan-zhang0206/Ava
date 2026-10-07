"""Register media for delivery through the exec turn's state update.

`ava.self.attach` appends each registration to `ava.state_update["attach"]`; the exec node takes
the entries out of the delta it commits, validates them, parks them in the `attach` channel of the
checkpointed state and turns them into one media message appended right after the exec output of the
registering turn (user ruling 2026-08-26). The SDK keeps no buffer of its own.
"""

from __future__ import annotations

from pathlib import Path

from ava.files import resolve
from ava.sdk_surface.validation import coerce_str
from base.lm.attach.constants import (
    ATTACH_MAX_FILE_BYTES,
    ATTACH_MAX_LABEL_CHARS,
    ATTACH_MEDIA_MIME,
)

# The graph-state channel a registration is appended to (`BaseAgentState.attach`).
_ATTACH_CHANNEL = "attach"


def media_gated_members(model: str) -> frozenset[str]:
    """Dotted ``ava`` member paths unavailable for `model`'s media capability
    — ``ava.self.attach`` on a text-only model (user ruling 2026-08-28). The
    help() renderer hides these from the SDK docs; empty for a media-capable
    model."""
    from base.lm.registry import resolve_available_model

    return _gated(_attach_unavailable_reason(resolve_available_model(model)))


def own_media_gated_members() -> frozenset[str]:
    """`media_gated_members` for the model of the process this runs in — the exec child, whose
    settings carry its agent's overlay."""
    return _gated(_attach_unavailable_reason())


def _gated(unavailable_reason: str | None) -> frozenset[str]:
    return frozenset() if unavailable_reason is None else frozenset({"ava.self.attach"})


def _current_model() -> str:
    """The agent's configured model resolved through any withdrawal fallback.

    Capability gates judge the model that will actually run, so a withdrawn id
    is gated as its fallback (task #3212)."""
    from ava.sdk_surface import settings as _settings
    from base.lm.registry import resolve_available_model

    return resolve_available_model(_settings.agent_setting("llm_model"))


def _attach_unavailable_reason(model: str | None = None) -> str | None:
    """Why ``attach`` is unavailable for `model` (default: this process's agent's), or None.

    A model with an empty attach-modality set (text-only, or an explicit empty
    ``attach_modalities`` declaration) cannot receive any attached media, so
    registering files for its next turn is a contradiction — the SDK docs drop
    the member and the call fails with this reason (user ruling 2026-08-28)."""
    from base.lm.registry import attach_modalities_for_model

    model = model or _current_model()
    if attach_modalities_for_model(model):
        return None
    return f"your model ({model}) is text-only and cannot receive media attachments"


def _validate_modality(suffix: str) -> None:
    """Reject a file whose modality the current model's attach set does not
    include — a clear error at registration, never a silent pack-time skip
    (user ruling 2026-08-28)."""
    from base.lm.registry import attach_modalities_for_model

    model = _current_model()
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
    import ava
    from ava.sdk_surface import process_context

    bound = process_context.peek()
    borrowed = bound is not None and bound.identity is not None and bound.identity.lease is not None
    if not ava.in_exec_turn() or borrowed:
        raise RuntimeError(
            "ava.self.attach only works inside an agent turn (execute_code); "
            "outside a turn there is no runner to deliver the attachment"
        )
    resolved = resolve(path).resolve()
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
    update = ava.state_update
    if not isinstance(update, dict):
        raise TypeError(
            f"ava.state_update must stay a dict, got {type(update).__name__} (attachment)"
        )
    update[_ATTACH_CHANNEL] = [
        *update.get(_ATTACH_CHANNEL, []),
        {"path": str(resolved), "label": label},
    ]

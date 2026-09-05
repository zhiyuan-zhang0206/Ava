"""Understand almost any input by asking a question about it.

One call handles text, images, video, audio, and PDF — you pass the material
plus a prompt and get back a text answer. You never see the raw bytes; the
prompt steers what comes back (summarize, extract, describe, answer).

The call is batch-shaped: you pass `targets` (a list of dicts, each with
`prompt` + exactly one of `text` / `paths`) and every target runs
concurrently, answers coming back in input order. A single question is a
one-element list. Same batch shape as `ava.web.fetch` / `ava.web.search`.

The module is also the importable home of the SDK entry point — the import
statement resolves without changing the attribute surface.

Provider split by modality: text (literal strings and text files) runs on the
text model (`settings.lm.understand_text_model`, default DeepSeek V4 Flash); binary
media (image / video / audio / PDF) runs on the media model
(`settings.lm.understand_media_model`, default Gemini 3.5 Flash, which natively
decodes those bytes). The media model's provider is picked by the model prefix
through the same model factory every LLM path uses, and
`AVA_UNDERSTAND_MEDIA_BASE_URL` can point the Gemini branch at a self-hosted
relay or mirror. The media wire format is Gemini-specific, so the path currently
supports Gemini models only — a non-Gemini media model fails fast with a clear
error; the abstraction (factory routing, endpoint override, quality knobs) is in
place so a second media provider can plug in with its own part conversion.
`effort` (default `max`) controls the answering model's reasoning depth; the
per-modality mapping is on the entry point below."""

from __future__ import annotations

__all_for_ava__ = [
    "UnderstandError",
    "understand",
]

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, cast, overload
from zoneinfo import ZoneInfo

from ava import files as _files
from ava._batch import DEFAULT_BATCH_MAX_CONCURRENT, run_batch, validate_max_concurrent
from ava._sdk_validation import coerce_str
from shared.config import settings
from shared.lm import provider_api
from shared.lm._call import invoke_text
from shared.lm._effort import (
    ReasoningEffort,
    _clamp_effort,
    coerce_effort,
)
from shared.lm.attach import ATTACH_MEDIA_MIME
from shared.lm.factory import provider_key_of_model

# Provider split by modality is config-driven: settings.lm.understand_text_model
# (default deepseek-v4-flash) handles literal strings / text files;
# settings.lm.understand_media_model (default gemini-3.5-flash) handles binary
# media. The default IDs live in shared/lm/factory.py:SUPPORTED_MODELS. The media
# model goes through the SAME provider factory as every other LLM path
# (`build_chat_model`); the Gemini client is the default media provider.

# Gemini inline-media cap (API docs: 20MB inline) — the default media provider's
# limit. Applies only to binary media read as bytes; the text path has no such
# cap.
_INLINE_MAX_BYTES = 20 * 1024 * 1024

# Suffix → MIME for the binary-media path. A file whose suffix is NOT listed
# here is read as UTF-8 text instead, so .txt/.md/.csv/.json/.py/... and
# extensionless files all flow through the text path.
_MEDIA_MIME = ATTACH_MEDIA_MIME

# Gemini media-resolution vocabulary (mapped by the gemini provider branch);
# validated here so an invalid setting fails fast with the setting's name
# before any model call.
_MEDIA_RESOLUTION_LEVELS = frozenset({"low", "medium", "high"})

# ── auto-save output to workspace ──────────────────────────────────────────
_OVERFLOW_DIRNAME = ".exec_output"
_OVERFLOW_KEEP = 20


def _save_understand_output(prompt: str, result: str, *, source: str) -> Path | None:
    """Save understand result to a timestamped file under the agent workspace's
    `.exec_output/` dir. Returns the file path, or None when no agent identity
    is established (outside an agent process). Prunes old files to a ring of
    `_OVERFLOW_KEEP`."""
    try:
        from ava import _boot
    except ImportError:
        return None
    try:
        agent_id = _boot.require_agent_id()
    except RuntimeError:
        return None
    from shared.paths import workspace_dir

    d = workspace_dir(agent_id) / _OVERFLOW_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    try:
        tz = ZoneInfo(settings.general.timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    slug = _slugify(prompt, max_len=40)
    path = d / f"understand_{now.strftime('%Y%m%d_%H%M%S_%f')}_{slug}.txt"
    content = "# understand result\n"
    content += f"# prompt: {prompt}\n"
    content += f"# source: {source}\n"
    content += f"# at: {now.isoformat()}\n\n"
    content += result
    path.write_text(content, encoding="utf-8")
    # Prune to the newest _OVERFLOW_KEEP, ordering by NAME rather than mtime.
    # The name carries `%Y%m%d_%H%M%S_%f` in fixed-width fields, so its lexical
    # order IS creation order at microsecond resolution. st_mtime is a weaker
    # measure of the same thing: filesystem timestamp granularity is coarse
    # enough that files written in quick succession share one, and the sort then
    # falls back to whatever order glob returned — making it unpredictable which
    # of the tied files gets deleted.
    existing = sorted(d.glob("understand_*.txt"))
    for old in existing[:-_OVERFLOW_KEEP]:
        old.unlink(missing_ok=True)
    return path


def _slugify(text: str, max_len: int = 40) -> str:
    """Turn a prompt into a safe filename slug."""
    import re

    s = text.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[-\s]+", "-", s)
    return s[:max_len].strip("-") or "result"


class UnderstandError(Exception):
    """`ava.understand` failure — API key not set, a file that is not valid
    UTF-8 text, media over the 20MB inline cap, upstream call failure, or an
    empty (safety-blocked) response. Catch it as
    `ava.understand.UnderstandError`. Filesystem errors (missing path,
    permissions) raise their usual exception types instead."""


@overload
def understand(
    targets: list[dict[str, str]],
    effort: str | ReasoningEffort = ReasoningEffort.MAX,
    max_concurrent: int | None = DEFAULT_BATCH_MAX_CONCURRENT,
) -> list[str]: ...


@overload
def understand(
    targets: list[dict[str, str | list[str]]],
    effort: str | ReasoningEffort = ReasoningEffort.MAX,
    max_concurrent: int | None = DEFAULT_BATCH_MAX_CONCURRENT,
) -> list[str]: ...


def understand[TargetValue: str | list[str]](
    targets: list[dict[str, TargetValue]],
    effort: str | ReasoningEffort = ReasoningEffort.MAX,
    max_concurrent: int | None = DEFAULT_BATCH_MAX_CONCURRENT,
) -> list[str]:
    """Answer a prompt about each target in parallel.

    Each target is a dict with `prompt` plus exactly one of `text` / `paths`.
    `paths` is a non-empty list of file paths (text, image, video, audio, or
    PDF). The files are sent together in ONE model call as separate parts;
    any media file makes the whole call run on the
    media model, and text files in the list ride along as text parts. `text`
    is the material itself as a literal string. A failed model call raises
    `ava.understand.UnderstandError`.

    `effort` sets the answering model's reasoning depth, one of `none` / `low`
    / `medium` / `high` / `xhigh` / `max` (also available as
    `ava.understand.ReasoningEffort`). Default `max` — the deepest reasoning
    the model supports. The media path maps it onto Gemini's
    `thinking_level` (no `max` level there): `max` keeps the configured
    `AVA_UNDERSTAND_MEDIA_THINKING_LEVEL` knob; other levels map to
    `minimal`/`low`/`medium`/`high` (`none` → `minimal`, `xhigh` → `high`).

    `max_concurrent` caps parallel targets; the default is 12. Pass a positive
    integer to choose a different ceiling.

    Every result is auto-saved to `.exec_output/` in your workspace; the path
    is logged at debug level.

    Returns:
        One answer per target, in input order.
    """
    if not isinstance(targets, list):
        raise TypeError(
            f"understand() takes a list of target dicts, got {type(targets).__name__}. "
            "Example: ava.understand([{'prompt': 'summarize this', 'paths': ['notes.md']}])"
        )
    for i, t in enumerate(targets):
        if not isinstance(t, dict):
            raise TypeError(
                f"targets[{i}] must be a dict, got {type(t).__name__}. "
                "Example: {'prompt': 'what is in this image', 'paths': ['shot.png']}"
            )
        if "prompt" not in t:
            raise ValueError(
                f"targets[{i}] missing required key 'prompt'. "
                "Example: {'prompt': 'summarize this', 'text': 'the material'}"
            )
        srcs = [k for k in ("text", "paths") if k in t]
        if len(srcs) != 1:
            raise ValueError(
                f"targets[{i}] must have exactly one of 'text' / 'paths', got {srcs}. "
                "Example: {'prompt': 'summarize this', 'paths': ['notes.md']}"
            )
        if "paths" in t:
            paths = t["paths"]
            if not isinstance(paths, list):
                raise TypeError(
                    f"targets[{i}]['paths'] must be a list of file paths, "
                    f"got {type(paths).__name__}. "
                    "Example: {'prompt': 'compare these', "
                    "'paths': ['design.png', 'shot.png']}"
                )
            if not paths:
                raise ValueError(
                    f"targets[{i}]['paths'] must not be empty — pass at least one file. "
                    "Example: {'prompt': 'compare these', "
                    "'paths': ['design.png', 'shot.png']}"
                )
            for j, p in enumerate(paths):
                if not isinstance(p, (str, Path)):
                    raise TypeError(
                        f"targets[{i}]['paths'][{j}] must be a path string or Path, "
                        f"got {type(p).__name__}"
                    )
    effort = coerce_str(effort, "effort")
    normalized = coerce_effort(effort, example="ava.understand(targets, effort='low')")
    assert normalized is not None  # noqa: S101 — understand's default is MAX, never None
    effort = normalized
    validate_max_concurrent(max_concurrent, example="ava.understand(targets, max_concurrent=4)")
    # The per-invoke bound lives in the provider client (build_chat_model
    # timeout) rather than a batch-layer wait_for: asyncio.run's loop close
    # waits for to_thread executor threads, so a wait_for timeout would not
    # return until the wedged SDK call finished anyway. The SDK timeout makes
    # the worker thread end on its own, and invoke_text's retry restarts the
    # bounded call.
    return asyncio.run(
        run_batch(
            targets,
            lambda t: _understand_one(
                prompt=cast(str, t["prompt"]),
                text=cast(str | None, t.get("text")),
                paths=cast(list[str | Path] | None, t.get("paths")),
                effort=effort,
            ),
            max_concurrent,
            after=_save_understand_result,
        )
    )


def _understand_one(
    *,
    prompt: str,
    text: str | None = None,
    paths: list[str | Path] | None = None,
    effort: str | ReasoningEffort = ReasoningEffort.MAX,
) -> str:
    """Synchronous unit of work — one prompt against one source.

    Split out so the asyncio layer can schedule multiple copies via
    `asyncio.to_thread`."""
    if text is not None:
        from ava.security import scan_content

        scan_content(text, source="understand.input")
        return _call_text(
            [{"type": "text", "text": text}, {"type": "text", "text": prompt}], effort=effort
        )
    assert paths is not None  # noqa: S101 — validated by caller
    return _understand_paths(paths, prompt, effort=effort)


def _read_text_file(p: Path) -> str:
    """Read `p` as UTF-8 text; undecodable bytes raise UnderstandError with
    the supported-suffix hint. Shared by the single-file and multi-file flows."""
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise UnderstandError(
            f"Could not read {str(p)!r} as UTF-8 text ({e}). "
            f"If this is binary media, its extension {p.suffix!r} is not recognized — "
            f"supported media suffixes: {sorted(_MEDIA_MIME)}."
        ) from e


def _understand_paths(paths: list[str | Path], prompt: str, effort: str | ReasoningEffort) -> str:
    """Answer `prompt` over several files in ONE model call.

    Media files (image/video/audio/PDF by suffix) become media parts; any
    other file is read as UTF-8 text and becomes a text part. If at least one
    part is media the whole call runs on the media model (the media provider
    handles text parts natively); an all-text list stays on the text model.
    Part order
    follows `paths` order, prompt last.
    """
    from ava.security import scan_content

    parts: list[Any] = []
    mimes: list[str] = []
    for path in paths:
        p = _files._resolve(path)
        if not p.is_file():
            raise FileNotFoundError(f"path {str(path)!r} does not name an existing file ({p})")
        mime = _MEDIA_MIME.get(p.suffix.lower())
        if mime is not None:
            data = p.read_bytes()
            if len(data) > _INLINE_MAX_BYTES:
                raise UnderstandError(
                    f"File size {len(data):,} bytes exceeds the {_INLINE_MAX_BYTES:,} byte "
                    "media inline cap (the default Gemini media provider's limit)"
                )
            parts.append({"type": "media", "data": data, "mime_type": mime})
            mimes.append(mime)
        else:
            content = _read_text_file(p)
            scan_content(content, source="understand.input")
            parts.append({"type": "text", "text": content})
            mimes.append("text/plain")
    parts.append({"type": "text", "text": prompt})
    if any(m != "text/plain" for m in mimes):
        return _call_media(parts, mime=",".join(dict.fromkeys(mimes)), effort=effort)
    return _call_text(parts, effort=effort)


def _save_understand_result(t: dict[str, str | list[str]], result: str) -> None:
    """Auto-save one target's result to `.exec_output/` — the `run_batch`
    `after` hook for `understand()`.

    Runs on the event-loop thread in input order (not inside the worker
    thread), so the timestamped filenames cannot interleave and the saved
    files sort the way the batch was submitted.
    """
    from ava.security import scan_content

    scan_content(result, source="understand.output")
    prompt = cast(str, t["prompt"])
    paths = t.get("paths")
    source = f"paths={paths!r}" if paths is not None else "text"
    _save_understand_output(prompt, result, source=source)


def _call_text(content: list[Any], *, effort: str | ReasoningEffort) -> str:
    """Answer over a text-only `content` list using settings.lm.understand_text_model.

    `effort` (validated by the public function) rides
    `build_chat_model(reasoning_effort=...)`; the cross-provider clamp in
    `shared/lm/_effort.py` maps it onto what the model's provider accepts
    (`max` → deepseek's max, `none` → reasoning off via the thinking switch).
    """
    from shared.lm.factory import build_chat_model

    model = settings.lm.understand_text_model
    try:
        llm = build_chat_model(
            model,
            reasoning_effort=effort,
            timeout=settings.lm.llm_invoke_timeout_seconds,
        )
    except RuntimeError as e:  # missing API key etc. — surface uniformly
        raise UnderstandError(str(e)) from e
    return invoke_text(
        llm,
        content,
        desc=f"{model}, text",
        error_type=UnderstandError,
        retry_attempts=settings.lm.llm_invoke_retry_attempts,
        retry_delay_seconds=settings.lm.llm_invoke_retry_delay_seconds,
        retry_max_delay_seconds=settings.lm.llm_invoke_retry_max_delay_seconds,
        provider=provider_key_of_model(model),
        model=model,
        usage_source="understand",
    )


def _call_media(content: list[Any], *, mime: str, effort: str | ReasoningEffort) -> str:
    """Answer over a media `content` list using settings.lm.understand_media_model.

    The provider is picked by the model's prefix through `build_chat_model`, the
    same factory every other LLM path uses — no Gemini SDK import lives here.
    Gemini gets its quality knobs (`media_resolution` /
    `media_thinking_level`) via the factory's media-path parameters, and
    `AVA_UNDERSTAND_MEDIA_BASE_URL` can point the Gemini endpoint at a
    self-hosted relay. The media wire format is Gemini-specific, so a
    non-Gemini model fails fast here with a clear error instead of crashing
    later at invoke time; the abstraction is in place for a second media
    provider to plug in with its own part conversion.

    `effort` maps onto Gemini's `thinking_level` via the cross-provider clamp:
    any level is clamped onto `minimal`/`low`/`medium`/`high` (`none` →
    `minimal`, `xhigh` → `high`). `max` has no gemini equivalent and keeps the
    configured `settings.lm.understand_media_thinking_level` knob instead, so
    default calls behave exactly as before. (The path is Gemini-only for now —
    see the module docstring — so the knob always applies.)
    """
    from shared.lm.factory import build_chat_model, provider_key_of_model

    model = settings.lm.understand_media_model
    # The media part shape is Gemini-specific (see the module docstring); only
    # the Gemini provider client understands it. Fail fast at build time with a
    # clear error instead of letting a non-Gemini client crash at invoke time.
    if provider_key_of_model(model) != "gemini":
        raise UnderstandError(
            f"media model {model!r} is not supported by ava.understand — the "
            "media path (image/video/audio/PDF) currently supports Gemini "
            "models only (their media wire format is the only one the factory "
            "clients accept). Set AVA_UNDERSTAND_MEDIA_MODEL to a gemini-* model "
            "or wait for a second media provider."
        )
    res = settings.lm.understand_media_resolution
    if res not in _MEDIA_RESOLUTION_LEVELS:
        raise UnderstandError(
            f"invalid AVA_UNDERSTAND_MEDIA_RESOLUTION={res!r} — "
            f"must be one of {sorted(_MEDIA_RESOLUTION_LEVELS)}"
        )

    thinking_level = settings.lm.understand_media_thinking_level
    if effort != ReasoningEffort.MAX:
        binding = next(
            binding
            for prefix, binding in provider_api.REGISTRY.bindings.items()
            if model.startswith(prefix)
        )
        levels = binding.effort_levels
        if levels is None:
            raise UnderstandError(
                f"media model {model!r} does not declare a reasoning-effort vocabulary"
            )
        thinking_level = _clamp_effort(effort, levels, target="gemini")
    try:
        llm = build_chat_model(
            model,
            reasoning_effort=effort,
            timeout=settings.lm.llm_invoke_timeout_seconds,
            media_resolution=res,
            media_thinking_level=thinking_level,
            base_url=settings.lm.understand_media_base_url,
        )
    except RuntimeError as e:  # missing API key etc. — surface uniformly
        raise UnderstandError(str(e)) from e
    return invoke_text(
        llm,
        content,
        desc=f"{model}, mime={mime}",
        error_type=UnderstandError,
        retry_attempts=settings.lm.llm_invoke_retry_attempts,
        retry_delay_seconds=settings.lm.llm_invoke_retry_delay_seconds,
        retry_max_delay_seconds=settings.lm.llm_invoke_retry_max_delay_seconds,
        provider=provider_key_of_model(model),
        model=model,
        usage_source="understand.media",
    )


# Hang the exception class off the function so agents can catch it as
# `ava.understand.UnderstandError` — `ava.understand` is the top-level function,
# there is no `ava.understand` submodule to host the class. Same for the effort
# enum, so agents can pass `ava.understand.ReasoningEffort.LOW` without knowing
# the shared module path.
understand.UnderstandError = UnderstandError  # type: ignore[attr-defined]
understand.ReasoningEffort = ReasoningEffort  # type: ignore[attr-defined]

"""Answer one prompt per target: the provider calls, the media rules and the saved output.

`meiva/understand/__init__.py` is the namespace function; it validates its arguments (`plans.py`),
builds the `UnderstandDeps` of this process (`wiring.py`) and calls `run`. Everything `run` reads
that is not its plan arrives in the deps - the model facts, the configured models and knobs, the
provider factory, the output directory, the clock, the path resolver and the injection scan - so a
test hands it a fake factory and the values it wants, and reads back what the fake received.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from base.host.env.agent_slices import ModelOverrides
from base.lm.attach.constants import ATTACH_MEDIA_MIME
from base.lm.catalog import ModelCatalog
from base.lm.effort import ReasoningEffort, validate_effort
from meiva.sdk_surface.batch import run_batch
from meiva.understand.errors import UnderstandError
from meiva.understand.plans import Plan

__all__ = [
    "OVERFLOW_DIRNAME",
    "RetryPolicy",
    "UnderstandDeps",
    "run",
]

# Provider split by modality is config-driven: settings.lm.understand_text_model
# (default deepseek-flash) handles literal strings / text files;
# settings.lm.understand_media_model (default gemini-3.5-flash) handles binary
# media. The default IDs live in the model catalog (`base/lm/catalog/__init__.py`). The media
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
OVERFLOW_DIRNAME = ".exec_output"
_OVERFLOW_KEEP = 20


@dataclass(frozen=True)
class RetryPolicy:
    """How a failed provider call is retried."""

    attempts: int
    delay_seconds: float
    max_delay_seconds: float


@dataclass(frozen=True)
class UnderstandDeps:
    """The dependencies of `run`, nothing else."""

    catalog: ModelCatalog
    overrides: ModelOverrides
    text_model: str
    media_model: str
    media_resolution: str
    media_thinking_level: str | None
    media_base_url: str | None
    llm_override: str
    timeout_seconds: float
    retry: RetryPolicy
    build_chat_model: Callable[..., Any]
    scan: Callable[[str, str], str]
    resolve: Callable[[str | Path], Path]
    output_dir: Callable[[], Path | None]
    now: Callable[[], datetime]


def run(plan: Plan, deps: UnderstandDeps) -> list[str]:
    """Answer every target of `plan`, in input order, and save each answer."""
    # The per-invoke bound lives in the provider client (build_chat_model
    # timeout) rather than a batch-layer wait_for: asyncio.run's loop close
    # waits for to_thread executor threads, so a wait_for timeout would not
    # return until the wedged SDK call finished anyway. The SDK timeout makes
    # the worker thread end on its own, and invoke_text's retry restarts the
    # bounded call.
    return asyncio.run(
        run_batch(
            plan.targets,
            lambda t: _understand_one(
                deps,
                prompt=cast(str, t["prompt"]),
                text=cast(str | None, t.get("text")),
                paths=cast(list[str | Path] | None, t.get("paths")),
                effort=plan.effort,
            ),
            plan.max_concurrent,
            after=lambda t, result: _save_result(deps, t, result),
        )
    )


def _understand_one(
    deps: UnderstandDeps,
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
        deps.scan(text, "understand.input")
        return _call_text(
            deps, [{"type": "text", "text": text}, {"type": "text", "text": prompt}], effort=effort
        )
    assert paths is not None  # noqa: S101 — validated by caller
    return _understand_paths(deps, paths, prompt, effort=effort)


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


def _understand_paths(
    deps: UnderstandDeps, paths: list[str | Path], prompt: str, effort: str | ReasoningEffort
) -> str:
    """Answer `prompt` over several files in ONE model call.

    Media files (image/video/audio/PDF by suffix) become media parts; any
    other file is read as UTF-8 text and becomes a text part. If at least one
    part is media the whole call runs on the media model (the media provider
    handles text parts natively); an all-text list stays on the text model.
    Part order
    follows `paths` order, prompt last.
    """
    parts: list[Any] = []
    mimes: list[str] = []
    for path in paths:
        p = deps.resolve(path)
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
            deps.scan(content, "understand.input")
            parts.append({"type": "text", "text": content})
            mimes.append("text/plain")
    parts.append({"type": "text", "text": prompt})
    if any(m != "text/plain" for m in mimes):
        return _call_media(deps, parts, mime=",".join(dict.fromkeys(mimes)), effort=effort)
    return _call_text(deps, parts, effort=effort)


def _save_result(deps: UnderstandDeps, t: dict[str, str | list[str]], result: str) -> None:
    """Auto-save one target's result to `.exec_output/` — the `run_batch`
    `after` hook for `understand()`.

    Runs on the event-loop thread in input order (not inside the worker
    thread), so the timestamped filenames cannot interleave and the saved
    files sort the way the batch was submitted.
    """
    deps.scan(result, "understand.output")
    prompt = cast(str, t["prompt"])
    paths = t.get("paths")
    source = f"paths={paths!r}" if paths is not None else "text"
    _save_output(deps, prompt, result, source=source)


def _slugify(text: str, max_len: int = 40) -> str:
    """Turn a prompt into a safe filename slug."""
    s = text.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[-\s]+", "-", s)
    return s[:max_len].strip("-") or "result"


def _save_output(deps: UnderstandDeps, prompt: str, result: str, *, source: str) -> Path | None:
    """Save understand result to a timestamped file under the agent workspace's
    `.exec_output/` dir. Returns the file path, or None when no agent identity
    is established (outside an agent process: `deps.output_dir()` is None). Prunes old
    files to a ring of `_OVERFLOW_KEEP`."""
    d = deps.output_dir()
    if d is None:
        return None
    d.mkdir(parents=True, exist_ok=True)
    now = deps.now()
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


def _call_text(deps: UnderstandDeps, content: list[Any], *, effort: str | ReasoningEffort) -> str:
    """Answer over a text-only `content` list using settings.lm.understand_text_model.

    `effort` (validated by the public function) rides
    `build_chat_model(reasoning_effort=...)`; the exact per-model validation in
    `base/lm/effort.py` rejects unsupported graded values
    (`max` → deepseek's max, `none` → reasoning off via the thinking switch).
    """
    from base.lm.call import invoke_text

    model = deps.text_model
    try:
        llm = deps.build_chat_model(
            model,
            catalog=deps.catalog,
            llm_override=deps.llm_override,
            overrides=deps.overrides,
            reasoning_effort=effort,
            timeout=deps.timeout_seconds,
        )
    except RuntimeError as e:  # missing API key etc. — surface uniformly
        raise UnderstandError(str(e)) from e
    return invoke_text(
        llm,
        content,
        desc=f"{model}, text",
        error_type=UnderstandError,
        retry_attempts=deps.retry.attempts,
        retry_delay_seconds=deps.retry.delay_seconds,
        retry_max_delay_seconds=deps.retry.max_delay_seconds,
        model=model,
        usage_source="understand",
        catalog=deps.catalog,
    )


def _call_media(
    deps: UnderstandDeps, content: list[Any], *, mime: str, effort: str | ReasoningEffort
) -> str:
    """Answer over a media `content` list using settings.lm.understand_media_model.

    The provider is picked by the model's prefix through `build_chat_model`, the
    same factory every other LLM path uses — no Gemini SDK import lives here.
    Gemini gets its quality knobs (`media_resolution` /
    `media_thinking_level`) via the factory's media-path parameters, and
    `MEIVA_UNDERSTAND_MEDIA_BASE_URL` can point the Gemini endpoint at a
    self-hosted relay. The media wire format is Gemini-specific, so a
    non-Gemini model fails fast here with a clear error instead of crashing
    later at invoke time; the abstraction is in place for a second media
    provider to plug in with its own part conversion.

    Graded efforts must be exact model-supported values. The SDK's historical
    default `max` selects the configured media thinking level; explicit `none`
    selects the model's lowest thinking level because Gemini cannot turn it off.
    """
    from base.lm.call import invoke_text
    from base.lm.factory import provider_key_of_model

    catalog = deps.catalog
    model = deps.media_model
    # The media part shape is Gemini-specific (see the module docstring); only
    # the Gemini provider client understands it. Fail fast at build time with a
    # clear error instead of letting a non-Gemini client crash at invoke time.
    if provider_key_of_model(model, catalog=catalog) != "gemini":
        raise UnderstandError(
            f"media model {model!r} is not supported by meiva.understand — the "
            "media path (image/video/audio/PDF) currently supports Gemini "
            "models only (their media wire format is the only one the factory "
            "clients accept). Set MEIVA_UNDERSTAND_MEDIA_MODEL to a gemini-* model "
            "or wait for a second media provider."
        )
    res = deps.media_resolution
    if res not in _MEDIA_RESOLUTION_LEVELS:
        raise UnderstandError(
            f"invalid MEIVA_UNDERSTAND_MEDIA_RESOLUTION={res!r} — "
            f"must be one of {sorted(_MEDIA_RESOLUTION_LEVELS)}"
        )

    thinking_level = deps.media_thinking_level
    if effort != ReasoningEffort.MAX:
        spec = catalog.models[model]
        levels = spec.effort_levels
        if not levels:
            raise UnderstandError(
                f"media model {model!r} does not declare a reasoning-effort vocabulary"
            )
        thinking_level = (
            levels[0]
            if effort == ReasoningEffort.NONE
            else validate_effort(effort, levels, target=model)
        )
    try:
        llm = deps.build_chat_model(
            model,
            catalog=catalog,
            llm_override=deps.llm_override,
            overrides=deps.overrides,
            reasoning_effort=effort,
            timeout=deps.timeout_seconds,
            media_resolution=res,
            media_thinking_level=thinking_level,
            base_url=deps.media_base_url,
        )
    except RuntimeError as e:  # missing API key etc. — surface uniformly
        raise UnderstandError(str(e)) from e
    return invoke_text(
        llm,
        content,
        desc=f"{model}, mime={mime}",
        error_type=UnderstandError,
        retry_attempts=deps.retry.attempts,
        retry_delay_seconds=deps.retry.delay_seconds,
        retry_max_delay_seconds=deps.retry.max_delay_seconds,
        model=model,
        usage_source="understand.media",
        catalog=catalog,
    )

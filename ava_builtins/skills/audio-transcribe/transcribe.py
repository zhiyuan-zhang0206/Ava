"""Audio transcription via OpenAI's hosted speech-to-text.

The transcription fallback for the `web-sources` feed adapters: when a feed
item has no captions (most non-YouTube sources, music videos, brand-new
uploads), pull the audio and transcribe it here. `web-sources:youtube` and the other
adapters call this on their `transcript is None` path.

Cloud-only by design (the user picked the hosted service over a local model):
default model `gpt-4o-transcribe` — OpenAI's most accurate transcription model
(lower WER than `whisper-1`). It returns text only (no segment timestamps); the
digest wants prose, so that is the right trade. Pass `model="whisper-1"` if you
later need SRT/VTT timestamps.

Handles the OpenAI **25 MB per-request limit**: audio is first transcoded to a
compact mono 16 kHz mp3 (16 kHz is the model's native rate, so this is lossless
for ASR while shrinking ~10x), and anything still over the limit (very long
talks) is split into time segments, each transcribed and concatenated.

Requires `ffmpeg` on PATH, `yt-dlp` (only when the source is a URL/video id),
and `OPENAI_API_KEY` in `~/.ava/.env` (read via `shared.config.settings`).
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from functools import cache as _cache
from pathlib import Path
from typing import Any

# OpenAI caps an upload at 25 MB; stay under it with a safety margin.
_OPENAI_MAX_BYTES = 25 * 1024 * 1024
_SAFE_MAX_BYTES = 24 * 1024 * 1024

# A per-request *duration* ceiling, independent of bytes: at 32 kbps a 24 MB
# chunk is ~100 min, which can exceed the transcription model's per-call
# processing budget even while under the size cap. Segment on whichever binds
# first (bytes or seconds). 15 min is comfortably safe.
_MAX_SEGMENT_SECONDS = 900
# A sanity ceiling on total audio so a mis-passed huge file can't run up an
# unbounded transcription bill; callers raise it explicitly to override.
_MAX_TOTAL_SECONDS = 4 * 3600

# A bare 11-char YouTube id or any http(s) URL is fetched via yt-dlp; anything
# else is treated as a local media path.
_RE_VIDEO_ID = re.compile(r"^[0-9A-Za-z_-]{11}$")


@_cache
def _client() -> Any:
    """OpenAI client keyed from `~/.ava/.env` (via settings). Imported lazily so
    the module loads without the SDK/key for callers that only need helpers."""
    from openai import OpenAI

    from shared.config import settings

    key = settings.lm.openai_api_key
    if key is None:
        raise RuntimeError("OPENAI_API_KEY not set (expected in ~/.ava/.env) — cannot transcribe")
    return OpenAI(api_key=key.get_secret_value())


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=True)  # noqa: S603 (constructed args, not shell-interpolated)


def _resolve_to_audio(source: str, workdir: Path) -> Path:
    """A local media file is returned as-is; a YouTube id / URL is downloaded to
    an mp3 via yt-dlp. (Video files are handled by the transcode step.)"""
    p = Path(source).expanduser()
    if p.exists():
        return p
    if not (_RE_VIDEO_ID.match(source) or source.startswith(("http://", "https://"))):
        raise FileNotFoundError(f"source is neither a local file nor a URL/video id: {source!r}")
    url = f"https://www.youtube.com/watch?v={source}" if _RE_VIDEO_ID.match(source) else source
    out = workdir / "src.%(ext)s"
    # --no-config: ignore user yt-dlp config that could write sidecars (info
    # json / thumbnail) which a glob might then mis-pick. We forced
    # --audio-format mp3, so the audio is exactly src.mp3.
    _run(["yt-dlp", "--no-config", "-x", "--audio-format", "mp3", "-o", str(out), url])
    got = workdir / "src.mp3"
    if not got.exists():
        raise RuntimeError(f"yt-dlp produced no mp3 audio for {source!r}")
    return got


def _audio_seconds(media: Path) -> float:
    r = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(media),
        ]
    )
    return float(r.stdout.strip())


def _prepare_chunks(media: Path, workdir: Path, max_bytes: int, max_segment_s: int) -> list[Path]:
    """Transcode to a compact mono 16 kHz mp3, then split into segments under
    *both* the byte cap and `max_segment_s` if either binds. Returns the ordered
    chunk paths to transcribe."""
    compact = workdir / "compact.mp3"
    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            str(media),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "32k",
            str(compact),
        ]
    )
    size = compact.stat().st_size
    duration = _audio_seconds(compact)
    if size <= max_bytes and duration <= max_segment_s:
        return [compact]

    # Fixed-bitrate, so bytes scale with time: a segment length whose byte-share
    # stays under the cap (0.9 margin), then clamped by the duration ceiling and
    # a sane floor.
    byte_seg = int(duration * max_bytes / size * 0.9) if size else max_segment_s
    seg_time = max(60, min(byte_seg, max_segment_s))
    parts_tmpl = str(workdir / "part_%03d.mp3")
    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            str(compact),
            "-f",
            "segment",
            "-segment_time",
            str(seg_time),
            "-c",
            "copy",
            parts_tmpl,
        ]
    )
    parts = sorted(workdir.glob("part_*.mp3"))
    if not parts:
        raise RuntimeError(f"ffmpeg segmentation produced no parts for {media}")
    oversize = [p.name for p in parts if p.stat().st_size > _OPENAI_MAX_BYTES]
    if oversize:
        raise RuntimeError(f"segment(s) still over the 25 MB API cap after split: {oversize}")
    return parts


def _transcribe_file(path: Path, *, model: str, language: str | None, prompt: str | None) -> str:
    kwargs: dict[str, Any] = {"model": model, "response_format": "text"}
    if language:
        kwargs["language"] = language
    if prompt:
        kwargs["prompt"] = prompt
    with path.open("rb") as f:
        return str(_client().audio.transcriptions.create(file=f, **kwargs)).strip()


def transcribe(
    source: str,
    *,
    model: str = "gpt-4o-transcribe",
    language: str | None = None,
    prompt: str | None = None,
    max_bytes: int = _SAFE_MAX_BYTES,
    max_segment_s: int = _MAX_SEGMENT_SECONDS,
    max_duration_s: float | None = _MAX_TOTAL_SECONDS,
) -> dict[str, Any]:
    """Transcribe `source` to plain text via OpenAI.

    `source` is a local audio/video file, a YouTube video id, or any media URL
    (downloaded via yt-dlp). Long audio is segmented under both the 25 MB API
    limit and `max_segment_s`, and the per-segment texts are concatenated.

    `max_duration_s` is a sanity ceiling on total audio (one API call per
    segment costs money) — raise it or pass `None` to override for a long job.

    Returns `{text, model, chunks, audio_seconds, source}`. `language` is an
    optional ISO-639-1 hint; `prompt` is optional context to bias spelling.
    """
    if max_bytes > _OPENAI_MAX_BYTES:
        raise ValueError(f"max_bytes {max_bytes} exceeds the OpenAI 25 MB limit")
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        media = _resolve_to_audio(source, workdir)
        seconds = _audio_seconds(media)
        if max_duration_s is not None and seconds > max_duration_s:
            raise ValueError(
                f"audio is {seconds:.0f}s, over max_duration_s={max_duration_s:.0f}; "
                f"pass a larger max_duration_s to override"
            )
        chunks = _prepare_chunks(media, workdir, max_bytes, max_segment_s)
        texts = [_transcribe_file(c, model=model, language=language, prompt=prompt) for c in chunks]
        return {
            "text": "\n".join(t for t in texts if t),
            "model": model,
            "chunks": len(chunks),
            "audio_seconds": round(seconds, 1),
            "source": source,
        }


# ---------------------------------------------------------------------------
# CLI entry point: `.venv/bin/python skills/audio-transcribe/transcribe.py <source> ...`
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import json

    p = argparse.ArgumentParser(
        description="Transcribe audio/video to text via OpenAI speech-to-text."
    )
    p.add_argument("source", help="Local media file, YouTube video ID, or media URL")
    p.add_argument(
        "--model",
        default="gpt-4o-transcribe",
        help="Transcription model (default: gpt-4o-transcribe)",
    )
    p.add_argument("--language", default=None, help="Optional ISO-639-1 language hint")
    p.add_argument(
        "--prompt", default=None, help="Optional context to bias spelling of proper nouns"
    )
    p.add_argument(
        "--max-bytes", type=int, default=_SAFE_MAX_BYTES, help="Max segment bytes, under 25 MB"
    )
    p.add_argument(
        "--max-segment-s",
        type=int,
        default=_MAX_SEGMENT_SECONDS,
        help="Max segment duration in seconds",
    )
    p.add_argument(
        "--max-duration-s",
        type=float,
        default=_MAX_TOTAL_SECONDS,
        help="Max total audio duration (safety ceiling)",
    )
    p.add_argument(
        "--json", action="store_true", help="Output full result as JSON instead of plain text"
    )
    args = p.parse_args()

    result = transcribe(
        args.source,
        model=args.model,
        language=args.language,
        prompt=args.prompt,
        max_bytes=args.max_bytes,
        max_segment_s=args.max_segment_s,
        max_duration_s=args.max_duration_s,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))  # noqa: T201
    else:
        print(result["text"])  # noqa: T201

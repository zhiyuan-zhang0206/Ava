"""YouTube feed-discovery adapter of the `web-sources` family.

Two stages, both driven by `yt-dlp` and both login-free:

  enum   — given a *source* (a followed channel, or a playlist for a
           "series task"), list its videos newest-first and return the ones
           not yet in the raw mirror. A channel enumerates through its
           "uploads" playlist (`UC...` -> `UU...`, 2nd char flipped).
  fetch  — given a video id, pull metadata + description + transcript (and
           optionally the video file) into the raw mirror at
           `$AVA_HOME/state/mirrors/youtube/<id>/`, mirroring the `*-fetch` skills.

`sync()` ties them together: resolve -> enum not-yet-mirrored -> fetch each
-> return the new items projected to the S1 schema shared by every
`web-sources` adapter (see `web-sources/SKILL.md`).

Two source modes, matching how the user consumes YouTube:
  - **follow** (a channel): steady-state incremental. The uploads playlist is
    reverse-chronological, so "new" = walk from the top until the stored
    last-seen video id. First run is capped to `initial_limit` so we don't
    backfill years of history.
  - **series task** (`backfill=True`, typically a playlist like a conference):
    a bounded one-shot — take every video (up to `max_scan`), no watermark stop.

Login-free, so this runs anywhere (not Mac-mini-bound). Pure stdlib +
`yt-dlp` on PATH; no `chrome` MCP, no `ava` SDK.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from functools import cache as _cache
from pathlib import Path
from typing import Any, Literal


def _default_root() -> Path:
    """Default raw-mirror dir `$AVA_HOME/state/mirrors/youtube/` — shared derived
    state doubling as the sync watermark; the per-content subdir is the stable
    content id, so a re-run reuses it (dedup)."""
    home = Path(os.environ.get("AVA_HOME", "~/.ava")).expanduser()
    return home / "state" / "mirrors" / "youtube"


# --------------------------------------------------------------------------- #
# Source resolution
# --------------------------------------------------------------------------- #

_RE_CHANNEL_ID = re.compile(r"^UC[0-9A-Za-z_-]{22}$")
_RE_UPLOADS_ID = re.compile(r"^UU[0-9A-Za-z_-]{22}$")
_RE_PLAYLIST_ID = re.compile(r"^(?:PL|FL|LL|OL|RD)[0-9A-Za-z_-]{10,}$")
_RE_VIDEO_ID = re.compile(r"^[0-9A-Za-z_-]{11}$")
_RE_HANDLE = re.compile(r"^@[0-9A-Za-z._-]+$")

SourceKind = Literal["channel", "playlist"]


@dataclass(frozen=True)
class Source:
    """A resolved enumeration target.

    `enum_playlist` is the playlist actually walked by `enum`: for a channel
    it is the uploads playlist; for a playlist source it is the playlist
    itself. `source_id` is the registry-stable id (the `UC...` channel id, or
    the `PL...` playlist id).
    """

    kind: SourceKind
    source_id: str
    enum_playlist: str
    title: str | None = None


def _uploads_playlist(channel_id: str) -> str:
    """`UCxxxx` -> `UUxxxx`: a channel's auto-generated uploads playlist."""
    return "UU" + channel_id[2:]


def resolve_source(spec: str) -> Source:
    """Resolve a user-facing spec to a :class:`Source`.

    Accepts a channel id (`UC...`), an uploads playlist (`UU...`), any
    playlist id (`PL.../FL.../...`), an `@handle`, or a full channel/playlist
    URL. Handles and URLs cost one `yt-dlp` metadata call to resolve; bare ids
    are resolved offline.
    """
    spec = spec.strip()

    if _RE_CHANNEL_ID.match(spec):
        return Source("channel", spec, _uploads_playlist(spec))
    if _RE_UPLOADS_ID.match(spec):
        # An uploads playlist *is* a channel feed; recover the UC id.
        channel_id = "UC" + spec[2:]
        return Source("channel", channel_id, spec)
    if _RE_PLAYLIST_ID.match(spec):
        return Source("playlist", spec, spec)

    # Handle or URL: ask yt-dlp for the channel/playlist object (no entries).
    if _RE_HANDLE.match(spec):
        url = f"https://www.youtube.com/{spec}"
    elif spec.startswith(("http://", "https://")):
        url = spec
    else:
        raise ValueError(f"unrecognized YouTube source spec: {spec!r}")

    meta = _ytdlp_json(url, flat=True, playlist_items="0")
    # A playlist URL resolves to a playlist; a channel/handle URL resolves to
    # the channel object whose `id` is the UC id.
    pid = meta.get("id", "")
    if _RE_PLAYLIST_ID.match(pid):
        return Source("playlist", pid, pid, title=meta.get("title"))
    channel_id = meta.get("channel_id") or pid
    if not _RE_CHANNEL_ID.match(channel_id):
        raise ValueError(f"could not resolve {spec!r} to a channel/playlist (got id={pid!r})")
    return Source("channel", channel_id, _uploads_playlist(channel_id), title=meta.get("channel"))


# --------------------------------------------------------------------------- #
# Enumeration
# --------------------------------------------------------------------------- #


@dataclass
class FeedItem:
    """A lightweight enum result (pre-fetch): id + permalink + what little
    metadata flat-playlist mode exposes. Real `published_at`/stats arrive at
    fetch time."""

    video_id: str
    url: str
    title: str | None = None
    position: int | None = None


def _video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def enum(
    source: Source,
    *,
    seen: Callable[[str], bool] | None = None,
    limit: int | None = None,
    max_scan: int = 200,
) -> tuple[list[FeedItem], bool]:
    """List videos from `source`, newest-first, that are not yet in the mirror.

    `seen(video_id)` is the mirror watermark: walking the uploads playlist from
    the top, collect until the first video that `seen` reports as already
    mirrored, then stop. With `seen=None` (a backfill / series task) nothing
    stops the walk. `limit` caps how many items to return; `max_scan` caps how
    deep we page (a safety bound for a huge channel).

    Returns `(items, hit_watermark)`. `hit_watermark` is True iff the walk
    stopped because it reached an already-mirrored video — i.e. a steady-state
    incremental run. False means the window ran out (or was `limit`-capped)
    without hitting the mirror: a first follow, or a gap larger than the window.
    The caller uses this to decide whether to cap a first run to a sane backfill.
    """
    meta = _ytdlp_json(
        f"https://www.youtube.com/playlist?list={source.enum_playlist}",
        flat=True,
        playlist_items=f"1:{max_scan}",
    )
    entries = meta.get("entries")
    if entries is None:
        raise ValueError(f"yt-dlp returned no 'entries' for playlist {source.enum_playlist!r}")

    out: list[FeedItem] = []
    hit_watermark = False
    for pos, e in enumerate(entries, start=1):
        vid = e.get("id")
        if not vid or not _RE_VIDEO_ID.match(vid):
            continue
        if seen is not None and seen(vid):
            hit_watermark = True
            break  # reached the mirror; everything below is already fetched
        out.append(FeedItem(video_id=vid, url=_video_url(vid), title=e.get("title"), position=pos))
        if limit is not None and len(out) >= limit:
            break
    return out, hit_watermark


# --------------------------------------------------------------------------- #
# Per-item fetch -> raw mirror
# --------------------------------------------------------------------------- #

_RE_VTT_TAG = re.compile(r"<[^>]+>")  # <00:00:00.160><c> ... </c> inline timing


def _iso_from_epoch(ts: int | float | None) -> str | None:
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(int(ts), tz=_dt.UTC).isoformat().replace("+00:00", "Z")


def _clean_vtt(vtt: str) -> str:
    """VTT -> plain prose: drop headers/cues/timestamps, strip inline `<c>`
    word-timing tags, and collapse YouTube's rolling-caption repeats.

    YouTube re-emits each caption line verbatim as the next scrolls in, so the
    duplication is *consecutive exact* lines — dropping those is sufficient and
    lossless (empirically the only repeat shape produced). A broader
    substring-containment dedup was tried and removed: it dropped zero extra
    lines on real captions yet risked deleting a legitimately distinct short
    line that happened to be a substring of its predecessor.
    """
    lines: list[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT" or "-->" in line:
            continue
        if line.startswith(("Kind:", "Language:", "NOTE")):
            continue
        # Strip inline timing tags, decode HTML entities (`&nbsp;`, `&#39;`,
        # `&amp;`), then fold the non-breaking spaces YouTube emits into
        # ordinary spaces and collapse runs.
        text = html.unescape(_RE_VTT_TAG.sub("", line))
        text = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
        if not text:
            continue
        if lines and lines[-1] == text:
            continue  # consecutive duplicate (rolling caption)
        lines.append(text)
    return "\n".join(lines)


def _fetch_transcript(video_id: str, *, prefer_lang: str = "en") -> tuple[str | None, str | None]:
    """Return `(transcript, kind)` where kind is "human" | "auto" | None.

    Prefer author-provided subs (cleaner), fall back to ASR auto-captions
    (near-universal). yt-dlp writes the VTT; we clean it to prose.
    """
    langs = f"{prefer_lang},{prefer_lang}-orig,{prefer_lang}-US,{prefer_lang}-GB"
    for kind, flag in (("human", "--write-subs"), ("auto", "--write-auto-subs")):
        with tempfile.TemporaryDirectory() as tmp:
            out_tmpl = str(Path(tmp) / "%(id)s.%(ext)s")
            cmd = [
                "yt-dlp",
                "--skip-download",
                flag,
                "--sub-langs",
                langs,
                "--sub-format",
                "vtt",
                "-o",
                out_tmpl,
                _video_url(video_id),
            ]
            subprocess.run(cmd, capture_output=True, text=True, check=False)
            vtts = sorted(Path(tmp).glob(f"{video_id}*.vtt"))
            if vtts:
                transcript = _clean_vtt(vtts[0].read_text(encoding="utf-8", errors="replace"))
                if transcript:
                    return transcript, kind
    return None, None


def _whisper_fallback(video_id: str) -> str | None:
    """Transcribe a caption-less video's audio via the sibling `audio-transcribe`
    skill (OpenAI). Opt-in through `fetch(whisper_fallback=True)`. A real failure
    (missing key / ffmpeg / network) propagates — only empty output yields None,
    so this never silently masks a transcription error."""
    import importlib.util

    mod_path = Path(__file__).parents[3] / "audio-transcribe" / "transcribe.py"
    spec = importlib.util.spec_from_file_location("audio-transcribe", mod_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load audio-transcribe skill at {mod_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.transcribe(video_id)["text"] or None


def fetch(
    video_id: str,
    *,
    root: Path | None = None,
    with_video: bool = False,
    want_transcript: bool = True,
    whisper_fallback: bool = False,
) -> dict[str, Any]:
    """Fetch one video into the raw mirror `$AVA_HOME/state/mirrors/youtube/<id>/`.

    Writes `post.json` (+ `post.md`, `transcript.txt`, and `video.mp4` when
    `with_video`). Returns the curated post dict. Metadata + transcript only
    by default — the digest does not need the video bytes.

    Transcript is **best-effort**: many videos legitimately have no captions
    (music, brand-new uploads, captions disabled), so a missing transcript is
    `None` with `transcript_kind=None`, not an error — the digest falls back to
    title + description. Metadata fetch, by contrast, fails fast (`check=True`).

    `whisper_fallback=True` (opt-in; needs `OPENAI_API_KEY` + ffmpeg) routes a
    caption-less video through the `audio-transcribe` skill, setting
    `transcript_kind="whisper"`. A real transcription failure propagates (it is
    not silently swallowed) — only "video genuinely has no captions" yields None.
    """
    if not _RE_VIDEO_ID.match(video_id):
        raise ValueError(f"not a YouTube video id: {video_id!r}")

    meta = _ytdlp_json(_video_url(video_id), flat=False)
    transcript, transcript_kind = (None, None)
    if want_transcript:
        transcript, transcript_kind = _fetch_transcript(video_id)
        if transcript is None and whisper_fallback:
            transcript = _whisper_fallback(video_id)
            transcript_kind = "whisper" if transcript else None

    post: dict[str, Any] = {
        "platform": "youtube",
        "video_id": video_id,
        "url": _video_url(video_id),
        "title": meta.get("title"),
        "description": meta.get("description"),
        "channel": {
            "id": meta.get("channel_id"),
            "name": meta.get("channel") or meta.get("uploader"),
            "handle": meta.get("uploader_id"),
            "url": meta.get("channel_url"),
        },
        "published_at": _iso_from_epoch(meta.get("timestamp")),
        "duration_s": meta.get("duration"),
        "tags": meta.get("tags") or [],
        "thumbnail": meta.get("thumbnail"),
        "stats": {
            "views": meta.get("view_count"),
            "likes": meta.get("like_count"),
            "comments": meta.get("comment_count"),
        },
        "transcript": transcript,
        "transcript_kind": transcript_kind,
        "fetched_at": _now_iso(),
        "fetched_via": f"youtube-feed (yt-dlp {_ytdlp_version()})",
    }

    # Download the video (opt-in) *before* save() so `post.json` records its
    # path; save() will (re)create the same outdir idempotently.
    if with_video:
        root_ = root or _default_root()
        outdir = root_ / video_id
        outdir.mkdir(parents=True, exist_ok=True)
        _download_video(video_id, outdir / "video.mp4")
        post["video_path"] = str(outdir / "video.mp4")
    save(post, root=root)
    return post


def save(post: dict[str, Any], root: Path | None = None) -> Path:
    """Persist a fetched post to `$AVA_HOME/state/mirrors/youtube/<video_id>/`."""
    video_id = post["video_id"]
    if not _RE_VIDEO_ID.match(video_id):
        raise ValueError(f"refusing to save, bad video id: {video_id!r}")
    root = root or _default_root()
    outdir = root / video_id
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "post.json").write_text(json.dumps(post, ensure_ascii=False, indent=2))
    (outdir / "post.md").write_text(_render_markdown(post))
    if post.get("transcript"):
        (outdir / "transcript.txt").write_text(post["transcript"])
    return outdir


def _render_markdown(post: dict[str, Any]) -> str:
    ch = post["channel"]
    lines = [
        f"# {post.get('title') or post['video_id']}",
        "",
        f"- **channel**: {ch.get('name')} ({ch.get('handle')})",
        f"- **url**: {post['url']}",
        f"- **published**: {post.get('published_at')}",
        f"- **duration_s**: {post.get('duration_s')}",
        f"- **stats**: {post.get('stats')}",
        f"- **transcript**: {post.get('transcript_kind') or 'none'}",
        "",
        "## Description",
        "",
        post.get("description") or "_(none)_",
    ]
    if post.get("transcript"):
        lines += ["", "## Transcript", "", post["transcript"]]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# S1 projection
# --------------------------------------------------------------------------- #


def to_s1(post: dict[str, Any], *, matrix_entity: str | None = None) -> dict[str, Any]:
    """Project a fetched post onto the cross-platform S1 item schema."""
    ch = post["channel"]
    media = []
    if post.get("video_path"):
        media.append({"kind": "video", "path": post["video_path"]})
    return {
        "platform": "youtube",
        "source_id": post["video_id"],
        "url": post["url"],
        "author": {"id": ch.get("id"), "name": ch.get("name")},
        "matrix_entity": matrix_entity,
        "title": post.get("title"),
        "text": post.get("description"),
        "transcript": post.get("transcript"),
        "published_at": post.get("published_at"),
        "fetched_at": post.get("fetched_at"),
        "fetched_via": post.get("fetched_via"),
        "media": media,
        "stats": post.get("stats", {}),
        "raw_path": str(_default_root() / post["video_id"]) + "/",
    }


# --------------------------------------------------------------------------- #
# Mirror watermark (the raw mirror IS the cursor — no separate state file)
# --------------------------------------------------------------------------- #


def _is_mirrored(video_id: str, root: Path | None) -> bool:
    """True iff `<root>/<video_id>/post.json` already exists (a complete mirror)."""
    root = root or _default_root()
    return (root / video_id / "post.json").exists()


# --------------------------------------------------------------------------- #
# sync — the high-level entrypoint
# --------------------------------------------------------------------------- #


@dataclass
class SyncResult:
    source: Source
    new_items: list[dict[str, Any]] = field(default_factory=list)  # S1 dicts
    enumerated: list[FeedItem] = field(default_factory=list)
    skipped_ids: list[str] = field(default_factory=list)  # already in the mirror


def sync(
    spec: str,
    *,
    backfill: bool = False,
    matrix_entity: str | None = None,
    initial_limit: int = 10,
    max_scan: int = 200,
    do_fetch: bool = True,
    with_video: bool = False,
    root: Path | None = None,
) -> SyncResult:
    """Resolve `spec`, enumerate what's not yet mirrored, fetch each.

    The raw mirror is the watermark: enum walks the uploads playlist from the top
    and stops at the first already-mirrored video. `backfill=True` is the "series
    task" mode: take every video (up to `max_scan`), no early stop, only skipping
    ones already mirrored. Otherwise it is a follow: a steady-state run stops at
    the mirror and takes everything above it; a first follow (or a gap larger than
    the window — enum never reached the mirror) is capped to `initial_limit` so it
    does not backfill the whole window.

    `do_fetch=False` is a non-mutating preview (enumerate only)."""
    source = resolve_source(spec)
    seen = None if backfill else (lambda vid: _is_mirrored(vid, root))
    items, hit_watermark = enum(source, seen=seen, max_scan=max_scan)
    if not backfill and not hit_watermark:
        items = items[:initial_limit]

    result = SyncResult(source=source, enumerated=items)
    if not do_fetch:
        return result

    for it in items:
        if _is_mirrored(it.video_id, root):  # only triggers under backfill (no early stop)
            result.skipped_ids.append(it.video_id)
            continue
        post = fetch(it.video_id, root=root, with_video=with_video)
        result.new_items.append(to_s1(post, matrix_entity=matrix_entity))
    return result


# --------------------------------------------------------------------------- #
# yt-dlp subprocess plumbing
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.UTC).isoformat().replace("+00:00", "Z")


@_cache
def _ytdlp_version() -> str:
    r = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _ytdlp_json(url: str, *, flat: bool, playlist_items: str | None = None) -> dict[str, Any]:
    """Run `yt-dlp -J` and parse the single JSON object it prints.

    `flat=True` adds `--flat-playlist` (ids+titles only, no per-video probe —
    fast for enumeration). `playlist_items` slices via `-I` (e.g. "1:200" to
    bound a walk, "0" for metadata only).
    """
    cmd = ["yt-dlp", "-J"]
    if flat:
        cmd.append("--flat-playlist")
    if playlist_items is not None:
        cmd += ["-I", playlist_items]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def _download_video(video_id: str, dest: Path) -> None:
    """Download the actual video file (opt-in; the digest rarely needs it)."""
    cmd = [
        "yt-dlp",
        "-f",
        "bv*+ba/b",
        "--merge-output-format",
        "mp4",
        "-o",
        str(dest),
        _video_url(video_id),
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=True)


# Re-export the dataclasses as plain dicts for callers that want JSON.
def item_asdict(it: FeedItem) -> dict[str, Any]:
    return asdict(it)

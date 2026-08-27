"""Hermetic unit tests for the web-sources:youtube skill (ava_builtins/skills/web-sources/youtube/reference/feed.py).

The skill's live behavior (real yt-dlp against real YouTube) was verified by
hand during the build; these lock the *pure* logic that regresses silently —
source resolution, the VTT->prose cleaner, the enum walk/mirror-stop, S1
projection, and the follow-vs-backfill mirror-derived semantics — by mocking the
yt-dlp subprocess so nothing hits the network.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

# Load the skill module by path (ava_builtins/skills/ are not importable packages). Register
# in sys.modules before exec so `@dataclass` can resolve the module by name.
_FEED_PATH = (
    Path(__file__).parents[2]
    / "ava_builtins"
    / "skills"
    / "web-sources"
    / "youtube"
    / "reference"
    / "feed.py"
)
_spec = importlib.util.spec_from_file_location("youtube_feed_under_test", _FEED_PATH)
assert _spec and _spec.loader
feed = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = feed
_spec.loader.exec_module(feed)


# --------------------------------------------------------------------------- #
# Source resolution (offline branches — no yt-dlp call)
# --------------------------------------------------------------------------- #


def test_channel_id_resolves_to_uploads_playlist() -> None:
    s = feed.resolve_source("UCdemodemodemodemodemo00")
    assert s.kind == "channel"
    assert s.source_id == "UCdemodemodemodemodemo00"
    assert s.enum_playlist == "UUdemodemodemodemodemo00"  # 2nd char flipped


def test_uploads_playlist_recovers_channel_id() -> None:
    s = feed.resolve_source("UUdemodemodemodemodemo00")
    assert s.kind == "channel"
    assert s.source_id == "UCdemodemodemodemodemo00"
    assert s.enum_playlist == "UUdemodemodemodemodemo00"


def test_playlist_id_resolves_to_itself() -> None:
    s = feed.resolve_source("PLf2m23nhTg1P5BsOHUOXyQz5RhfUSSVUi")
    assert s.kind == "playlist"
    assert s.source_id == s.enum_playlist == "PLf2m23nhTg1P5BsOHUOXyQz5RhfUSSVUi"


def test_bad_spec_raises() -> None:
    with pytest.raises(ValueError):
        feed.resolve_source("not a url or id")


def test_handle_resolves_via_ytdlp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        feed,
        "_ytdlp_json",
        lambda *a, **k: {  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
            "id": "@x",
            "channel_id": "UCdemodemodemodemodemo22",
            "channel": "Anthropic",
        },
    )
    s = feed.resolve_source("@demo-channel")
    assert s.kind == "channel"
    assert s.source_id == "UCdemodemodemodemodemo22"
    assert s.title == "Anthropic"


# --------------------------------------------------------------------------- #
# VTT cleaning
# --------------------------------------------------------------------------- #


def test_clean_vtt_strips_tags_and_entities() -> None:
    vtt = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.910 align:start position:0%
The<00:00:00.160><c> important</c> is your code good&#39;s&nbsp;value
"""
    out = feed._clean_vtt(vtt)
    assert "<c>" not in out and "<00:" not in out
    assert "&nbsp;" not in out and "&#39;" not in out and "\xa0" not in out
    assert out == "The important is your code good's value"


def test_clean_vtt_dedups_consecutive_exact_only() -> None:
    # consecutive exact duplicate -> collapsed. A later distinct line is kept
    # even when it is a substring of the previous one ("hello" after "hello
    # world foo") — the old substring-containment dedup wrongly dropped it.
    vtt = """WEBVTT

00:00:00.000 --> 00:00:02.000
hello world

00:00:02.000 --> 00:00:04.000
hello world

00:00:04.000 --> 00:00:06.000
hello world foo

00:00:06.000 --> 00:00:08.000
hello
"""
    assert feed._clean_vtt(vtt) == "hello world\nhello world foo\nhello"


def test_clean_vtt_empty() -> None:
    assert feed._clean_vtt("WEBVTT\n\n") == ""


# --------------------------------------------------------------------------- #
# enum walk / mirror stop / limit / bad-id skip
# --------------------------------------------------------------------------- #


def _fake_playlist(ids: list[str]) -> dict[str, Any]:
    return {"entries": [{"id": vid, "title": f"t-{vid}"} for vid in ids]}


def test_enum_stops_at_watermark(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"]
    monkeypatch.setattr(feed, "_ytdlp_json", lambda *a, **k: _fake_playlist(ids))  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    src = feed.Source("channel", "UC" + "x" * 22, "UU" + "x" * 22)
    items, hit = feed.enum(src, seen=lambda v: v == "ccccccccccc")
    assert [i.video_id for i in items] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]  # stop at c
    assert hit is True  # reached the mirror


def test_enum_limit_and_skips_bad_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = ["aaaaaaaaaaa", "tooShort", "bbbbbbbbbbb", "ccccccccccc"]
    monkeypatch.setattr(feed, "_ytdlp_json", lambda *a, **k: _fake_playlist(ids))  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    src = feed.Source("channel", "UC" + "x" * 22, "UU" + "x" * 22)
    items, hit = feed.enum(src, limit=2)
    assert [i.video_id for i in items] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]  # bad id skipped, cap 2
    assert hit is False  # limit-cut, not a watermark stop


def test_enum_no_watermark_when_none_mirrored(monkeypatch: pytest.MonkeyPatch) -> None:
    # First follow (mirror empty): the walk runs out without hitting the mirror,
    # so hit_watermark is False and the caller caps it to a sane backfill.
    ids = ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
    monkeypatch.setattr(feed, "_ytdlp_json", lambda *a, **k: _fake_playlist(ids))  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    src = feed.Source("channel", "UC" + "x" * 22, "UU" + "x" * 22)
    items, hit = feed.enum(src, seen=lambda v: False)  # noqa: ARG005
    assert [i.video_id for i in items] == ids
    assert hit is False


def test_enum_raises_on_missing_entries_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feed, "_ytdlp_json", lambda *a, **k: {"id": "UUxxx"})  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    src = feed.Source("channel", "UC" + "x" * 22, "UU" + "x" * 22)
    with pytest.raises(ValueError, match="no 'entries'"):
        feed.enum(src)


# --------------------------------------------------------------------------- #
# S1 projection
# --------------------------------------------------------------------------- #


def test_to_s1_shape() -> None:
    post = {
        "video_id": "dRsjO-88nBs",
        "url": "https://www.youtube.com/watch?v=dRsjO-88nBs",
        "title": "T",
        "description": "D",
        "channel": {"id": "UCdemodemodemodemodemo22", "name": "\u793a\u4f8b\u9891\u9053"},
        "published_at": "2025-07-31T18:22:30Z",
        "transcript": "hello",
        "fetched_at": "2026-06-06T00:00:00Z",
        "fetched_via": "youtube-feed (yt-dlp X)",
        "stats": {"views": 1},
    }
    s1 = feed.to_s1(post, matrix_entity="quantbit")
    assert s1["platform"] == "youtube"
    assert s1["source_id"] == "dRsjO-88nBs"
    assert s1["author"] == {"id": "UCdemodemodemodemodemo22", "name": "\u793a\u4f8b\u9891\u9053"}
    assert s1["matrix_entity"] == "quantbit"
    assert s1["text"] == "D" and s1["transcript"] == "hello"
    assert s1["media"] == []  # no video downloaded
    assert s1["raw_path"].endswith("/dRsjO-88nBs/")


# --------------------------------------------------------------------------- #
# sync: follow (incremental, capped on first run) vs series-task backfill
# --------------------------------------------------------------------------- #


def _fetch_stub(vid: str, **_k: Any) -> dict[str, Any]:
    return {"video_id": vid, "channel": {}, "stats": {}}


def _stub_sync(
    monkeypatch: pytest.MonkeyPatch,
    ids: list[str],
    *,
    hit: bool,
    mirrored: set[str] | None = None,
) -> None:
    """enum returns (`ids`, `hit`) verbatim (the real enum's seen-stop is exercised
    elsewhere); `_is_mirrored` reports the `mirrored` set; fetch/to_s1 echo the id."""
    seen = mirrored or set()
    monkeypatch.setattr(
        feed,
        "resolve_source",
        lambda spec: feed.Source("channel", "UC" + "x" * 22, "UU" + "x" * 22),  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        feed,
        "enum",
        lambda src, **k: ([feed.FeedItem(v, feed._video_url(v)) for v in ids], hit),  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(feed, "_is_mirrored", lambda vid, root: vid in seen)  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(feed, "fetch", _fetch_stub)
    monkeypatch.setattr(feed, "to_s1", lambda post, **k: {"source_id": post["video_id"]})  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]


def test_sync_incremental_fetches_all_new(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Steady state: enum hit the mirror, so every returned (new) item is fetched.
    _stub_sync(monkeypatch, ["newest12345", "older123456"], hit=True)
    r = feed.sync("UCabc", root=tmp_path)
    assert [i["source_id"] for i in r.new_items] == ["newest12345", "older123456"]
    assert r.skipped_ids == []


def test_sync_first_run_caps_to_initial_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # First follow (enum never hit the mirror): cap to initial_limit, don't backfill.
    _stub_sync(monkeypatch, ["v1234567890", "v2234567890", "v3234567890"], hit=False)
    r = feed.sync("UCabc", initial_limit=2, root=tmp_path)
    assert [i["source_id"] for i in r.new_items] == ["v1234567890", "v2234567890"]


def test_sync_backfill_skips_mirrored_no_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Series task: no early stop, no cap; already-mirrored videos are skipped.
    _stub_sync(
        monkeypatch,
        ["a1234567890", "b1234567890", "c1234567890"],
        hit=False,
        mirrored={"b1234567890"},
    )
    r = feed.sync("PLxxxx", backfill=True, initial_limit=1, root=tmp_path)
    assert [i["source_id"] for i in r.new_items] == ["a1234567890", "c1234567890"]
    assert r.skipped_ids == ["b1234567890"]


def test_sync_preview_no_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_sync(monkeypatch, ["newest12345", "older123456"], hit=True)
    r = feed.sync("UCabc", do_fetch=False, root=tmp_path)
    assert len(r.enumerated) == 2
    assert r.new_items == []


# --------------------------------------------------------------------------- #
# whisper fallback wiring (no-caption -> audio-transcribe)
# --------------------------------------------------------------------------- #


def test_fetch_whisper_fallback_fills_caption_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feed, "_ytdlp_json", lambda *a, **k: {"title": "T", "channel": "C"})  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(feed, "_ytdlp_version", lambda: "x")
    monkeypatch.setattr(feed, "_fetch_transcript", lambda vid: (None, None))  # noqa: ARG005 — no captions  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(feed, "_whisper_fallback", lambda vid: "whispered text")  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    post = feed.fetch("dRsjO-88nBs", root=tmp_path, whisper_fallback=True)
    assert post["transcript"] == "whispered text"
    assert post["transcript_kind"] == "whisper"


def test_fetch_no_whisper_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feed, "_ytdlp_json", lambda *a, **k: {"title": "T", "channel": "C"})  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(feed, "_ytdlp_version", lambda: "x")
    monkeypatch.setattr(feed, "_fetch_transcript", lambda vid: (None, None))  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    called = False

    def _boom(vid: str) -> str | None:
        nonlocal called
        called = True
        return "should not be called"

    monkeypatch.setattr(feed, "_whisper_fallback", _boom)
    post = feed.fetch("dRsjO-88nBs", root=tmp_path)  # whisper_fallback defaults False
    assert post["transcript"] is None and post["transcript_kind"] is None
    assert called is False

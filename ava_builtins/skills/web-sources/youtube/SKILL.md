---
name: youtube
description: Enumerates YouTube channel or playlist videos and fetches metadata and transcripts without login. Use when following YouTube updates, ingesting a channel, playlist, conference series, or single video, or checking what is new.
---

# youtube

The **YouTube adapter** of the `web-sources` family (shared S1 item schema: [`../SKILL.md`](../SKILL.md)).

This fills the **feed-discovery layer** (what's new in the followed accounts), not single-item fetch.
YouTube is the only source where **both enum and fetch require no login** — pure `yt-dlp` subprocess,
no `chrome` MCP, no `ava` SDK needed, not tied to a specific machine, can run on CI/any machine.

## Two source modes (corresponding to two consumption methods)

| Mode | Typical target | Entry | Watermark |
|---|---|---|---|
| **Follow** (channel) | Example channel / some official channel | `sync(spec)` | Incremental: walk the uploads list from the top until hitting a video **already in mirror**; first follow (no mirror hit) truncates to `initial_limit` items |
| **Series task** (playlist) | Conference recordings e.g. Code w/ Claude | `sync(spec, backfill=True)` | None: fetch all at once (≤`max_scan`), skip already mirrored |

Both reduce to the same primitive — enumerate a playlist newest-first, stopping when hitting an already mirrored item.
A channel is just syntactic sugar for its uploads list (`UC...` → `UU...`, flipping the 2nd character).

## Accepted forms of source spec

`resolve_source(spec)` accepts: channel id (`UC...`) / uploads list (`UU...`) /
any playlist (`PL.../FL.../...`) / `@handle` / full channel or playlist URL.
Bare IDs are resolved offline; handles / URLs cost one yt-dlp call to resolve.

## Usage

```python
import os
import sys
sys.path.insert(0, os.path.join(os.environ["AVA_HOME"], "skills", "web-sources", "youtube", "reference"))
import feed

# Follow a channel: incremental pull of new items, each fetched to raw mirror + projected S1.
# The watermark is the raw mirror itself
# ($AVA_HOME/state/mirrors/youtube/<id>/), next time only fetch those not yet mirrored, no separate cursor file.
r = feed.sync("UCdemodemodemodemodemo00")          # Example channel (UC id)
r = feed.sync("@demo-channel")                      # or handle / channel URL
for s1 in r.new_items:
    print(s1["title"], s1["url"])

# Series task: backfill an entire conference playlist (no early stop, skip already mirrored).
r = feed.sync("PLf2m23nhTg1P5BsOHUOXyQz5RhfUSSVUi", backfill=True)

# Only list, don't fetch (preview how many new):
r = feed.sync("@demo-handle", do_fetch=False)
print(len(r.enumerated), "new")

# Single-item fetch (directly fetch one outside enum):
post = feed.fetch("dRsjO-88nBs")                    # metadata + description + transcript
post = feed.fetch("dRsjO-88nBs", with_video=True)   # additionally download video file (default is no)
```

`sync` full process: resolve → enumerate until hitting already mirrored → fetch unmirrored items one by one →
return `SyncResult` (`new_items` is a list of S1 dicts, `skipped_ids` are the IDs already in mirror that were skipped).

## Output schema

```
$AVA_HOME/state/mirrors/youtube/<video_id>/
├── post.json       # Curated metadata (see below) + transcript
├── post.md         # Rendered version (channel / stats / description / transcript)
├── transcript.txt  # Plain text transcript (when captions available)
└── video.mp4       # Only when with_video=True
```

`post.json` fields: `platform / video_id / url / title / description /
channel{id,name,handle,url} / published_at(ISO8601) / duration_s / tags /
thumbnail / stats{views,likes,comments} / transcript / transcript_kind /
fetched_at / fetched_via`. `to_s1(post)` projects this into the family's cross-platform S1 item:
`platform="youtube"`, `source_id` = video id, **`author.id` = channel id** (the stable follow key),
`text` = description, `stats` carried through, `media` = `[{kind:"video", path}]` only with `with_video=True`.

## transcript

`yt-dlp` writes subtitles, preferring **author-uploaded subtitles** (`--write-subs`, clean), falling back to
**ASR auto-generated subtitles** (`--write-auto-subs`, near full coverage). VTT is cleaned: remove `<c>`
per-word timing tags, decode HTML entities (`&nbsp;`/`&#39;`), remove YouTube scroll subtitle duplicate lines →
pure prose saved to `transcript.txt` and `post.json.transcript`. `transcript_kind` =
`human` / `auto` / `whisper` / `null`.

**Data API captions are useless for us**: `captions.download` requires you to **own** the video,
you can't pull from followed third-party channels — so transcript only goes through yt-dlp.
When no captions (music video / just published a few minutes ago / author disabled captions) `transcript=null`,
digest falls back to title+description.

**No-captions fallback (opt-in)**: `fetch(..., whisper_fallback=True)` when yt-dlp fails to get captions,
uses [`audio-transcribe`](../../audio-transcribe/SKILL.md) to extract audio and do OpenAI transcription,
`transcript_kind="whisper"`. Requires `OPENAI_API_KEY` + ffmpeg; if truly fails, it raises (not silently swallowed).
Default is off, so without configuring the key it runs fine (just leaves `null` for videos without captions).

## Who to follow — is memory, not this skill

This skill only teaches **how to do** (given a channel/playlist, enumerate its new videos).
**Which channels to follow is your memory** — residing in a note in the memory pool,
not in this skill repo. The digest agent reads its own memory to know who to follow,
then calls `feed.sync(spec)` for each.

```python
# Typical usage of digest agent: get channel list from memory, sync each
for spec in my_youtube_follows:        # ← from agent's memory note, not this skill
    r = feed.sync(spec)
    ...
```

**One-off bootstrap (optional)**: the YouTube Data API's `subscriptions.list` (OAuth `youtube.readonly`)
can export "all channels I follow" in one go, to populate that memory note.
yt-dlp cannot do this (subscriptions are account-private). This is a one-time bootstrap;
steady-state digest requires no OAuth. Recognizing that a channel and some other platform's account are
the **same real-world source** is likewise the agent's job, driven by what its memory records — pass that
judgement down as the S1 `matrix_entity` tag so downstream dedup can act on it.

## Watermark (raw mirror is the watermark, no cursor file)

Whether seen or not is derived directly from the raw mirror — a video is new if and only if
`$AVA_HOME/state/mirrors/youtube/<id>/post.json` does not exist yet.
**There is no `state/` cursor file** (to avoid a second source of truth that can drift from the mirror).
Enumeration walks the uploads list (reverse-chron) until the first already mirrored item;
the enumeration reports "whether it hit a mirror": hit = steady-state incremental (fully fetched),
no hit (first follow / gap larger than window) = truncate to `initial_limit`, not backfill the entire window.
`backfill=True` does not stop early, only skips already mirrored items.

## Maintenance tax (known fragility of the yt-dlp no-API path)

Zero auth, zero quota, but yt-dlp scrapes the watch/innertube surface, and will break when YouTube changes its player.
During probing we've seen `use impersonation` warnings and `[jsc:deno] Solving JS challenges`
(yt-dlp now shells out to deno to solve player challenges). Mitigation: pin a recent yt-dlp
and regularly `pip install -U yt-dlp` (it self-updates frequently for YT changes);
when signature errors occur, install impersonation extra / deno.
Treat yt-dlp occasional breakage as a **known maintenance tax**, not a one-time configuration.

## Fields NOT fetched

- **Comment content**: only fetch `comment_count`, not comment text.
- **Video file**: not downloaded by default (`with_video=False`) — digest needs metadata + transcript,
  not hundreds of MB of bytes. Turn on `with_video=True` when you need the original video.
- **Member-only videos / private playlists**: cannot be obtained via the no-login path; out of digest scope.
- **Non-English transcript translations**: only fetch source language + en; `xx-en` machine-translated tracks
  exist but are low quality, not pulled by default.

## Troubleshooting

- **`yt-dlp` not found**: install `yt-dlp` (`uv tool install yt-dlp` or `pipx install yt-dlp`),
  confirm in PATH. Version is recorded in `fetched_via`.
- **Signature error / player challenge failure**: `pip install -U yt-dlp`; install deno or
  impersonation extra (see maintenance tax).
- **enum missing videos (gap larger than window)**: new content exceeds `max_scan` depth
  (channel posted >200 items at once, or haven't synced in a while), enum didn't hit mirror,
  will truncate to `initial_limit`; to fill the middle part, increase `max_scan` or use
  `backfill=True` to backfill the whole segment.
- **transcript is empty but video has captions**: just published a few minutes ago, auto captions
  not yet generated; or captions language is not en — pass `prefer_lang` to `_fetch_transcript`
  to try the source language.

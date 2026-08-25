---
name: web-sources
description: Fetches internet pages, posts, videos, feeds, and creator updates through source-specific adapters. Use when following a source, checking recent updates, ingesting web content, or retrieving a URL; always choose and load the matching adapter.
---

# web-sources —— Internet Content Fetching

Root skill of the feed-ingest **adapter family**. Each adapter below is a self-contained CLI for one platform/protocol.
They all do the same two things in the same shape: land what they fetch in a raw mirror directory under
`$AVA_HOME/state/mirrors/` (holding `post.json` + `post.md` + any downloaded bytes — each adapter's SKILL.md gives
its exact path), and project it onto the one **S1** item schema defined at the bottom of this file — so downstream consumers
(label / digest / converge) read S1 only and never care which adapter produced an item.

**First choose an adapter, then read its own SKILL.md** (`ava.help(ava.skills.web_sources.<adapter>)`).

## Which adapter to choose

| adapter | source | access method | login state |
|---|---|---|---|
| `youtube` | YouTube channel/playlist + transcript | yt-dlp, any machine | no login |
| `rss` | any RSS/Atom feed (blog/media/official blog/major newspaper) | curl + feedparser | no login |
| `generic` | **sources without platform API or RSS** (official site/forum/newsletter) | curl+trafilatura, fallback to Jina rendering on block | no login |

> Every adapter shipped here is **login-free** by design, so it runs on any machine and needs no personal account.
> Adapters for sources that require a logged-in browser or a personal account (social platforms, walled communities)
> are deployment-instance-specific and are **not shipped in this repository** — install them as user skills under
> `$AVA_HOME/skills/` if a deployment needs them. One account-bound source does ship, but as its own top-level skill
> rather than a sub-adapter here: [`gmail`](../gmail/SKILL.md) (IMAP/SMTP; its `enum`/`sync` emit the same S1 items).

`generic` is the fallback: if there's a platform account, use the dedicated adapter; for standard feeds use `rss`; **only when neither exists** fall back to `generic`
(provide a list page + `--link-pattern` to enumerate articles, or directly `fetch --url` to grab a single page's body text).

## Two actions (almost every adapter has)

- **enum / sync** —— Track a **source's update stream** (a creator's homepage, a feed URL): list/persist recent entries, mirror dedup, project to S1. This is the "subscription" semantic.
- **fetch** —— Fetch **a single URL / single item** (a video, a post, an article). This is the "fetch this one" semantic.

## Which sources to follow = user preference, not here

This skill only provides the **how to fetch** capability. **Whom to follow, which feeds to subscribe to, which sites to fetch** are the user's persistent preferences —
read from your own **memory (the followed-source list)**, and call the corresponding adapter's sync for each.
Do not hardcode any specific source names into this skill repository.

## Calling conventions (agent launches bash)

> **Environment**: scripts run from the load dir (`$AVA_HOME/skills/`) with the
> checkout's venv (`$AVA_HOME/source/.venv/bin/python`; in a dev checkout use
> `<checkout>/.venv/bin/python` instead). `$AVA_HOME` is set in agent processes.
```bash
# Most adapters' CLI is in reference/feed.py:
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-sources/<adapter>/reference/feed.py <enum|sync|fetch> --...
# youtube's CLI is in reference/ too (all adapters share the layout):
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-sources/youtube/reference/feed.py <...> --...
```

Each adapter's subcommands, parameters, cursor model, and output schema differ — **before you start, read its SKILL.md**.

## S1 contract (shared by all adapters)

Each adapter persists a platform-shaped `post.json` in its mirror, then projects it through `to_s1()` into the
**same** dict. That dict is S1 — every adapter emits exactly these keys, so downstream code consumes any source uniformly:

| field | meaning |
|---|---|
| `platform` | which adapter produced the item (`rss` / `web` / `youtube` / `gmail`) |
| `source_id` | the platform's own durable id for this item (entry guid / page URL / BV号 / video id / Message-Id) |
| `url` | canonical web permalink, or `null` when the source has none |
| `author` | `{id, name}` — **`id` is the durable follow key** (feed URL / site domain / uploader mid / channel id / List-Id), so all items of one source share it |
| `matrix_entity` | optional caller-supplied tag meaning "same real-world source seen across platforms"; `null` unless the caller passes `--matrix-entity` |
| `title` / `text` | headline + body prose (`text` = description, summary, or extracted article body, whichever the platform gives) |
| `transcript` | speech-to-text or captions for audio/video items, `null` for text sources or when transcription was not requested |
| `published_at` / `fetched_at` | ISO8601 source publication time / when this run fetched it |
| `fetched_via` | which path produced it (e.g. `web-scrape+jina`, the yt-dlp version) — provenance for debugging a bad item |
| `media` | `[{kind, path}]` for downloaded image/video files; `[]` when nothing was downloaded |
| `stats` | platform engagement counters (views / likes / comments / …); `{}` for platforms that have none |
| `raw_path` | the mirror directory this item was persisted to |

Two invariants worth relying on: **the mirror is the cursor** — an item counts as already seen iff its
`raw_path/post.json` exists, so a re-run is idempotent and there is no second state file to drift from the bytes on
disk; and **`author.id` is the follow key** — it is what a memory note should record when tracking a source, not the
per-item `source_id`. The authoritative binding of each field is the adapter's own `to_s1()` in its `feed.py`.

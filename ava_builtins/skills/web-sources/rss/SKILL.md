---
name: rss
description: "Parses RSS and Atom feeds, lists entries, and ingests full article text. Use when following a blog, news outlet, official feed, or feed URL, or when a source's updates stop appearing."
---

# rss

The **RSS/Atom adapter** of the `web-sources` family (shared S1 item schema: [`../SKILL.md`](../SKILL.md)).
Many sources (media official websites, AI lab official blogs, major newspapers, YouTube channels, personal blogs) expose standard RSS/Atom feeds, which is the cleanest integration method: **one feed URL is one collection**, with no need to enumerate followed accounts.
Which specific feeds to subscribe to are **user preferences** and are not written into this skill — the agent reads from its own memory (followed source list) and syncs one by one.

**A self-contained CLI script** (`reference/feed.py`): agent starts bash, calls with command-line arguments, reads stdout JSON.
Parsing uses `feedparser` (handles all RSS 0.9x/1.0/2.0 + Atom dialects + date normalization + `content:encoded`);
feed bytes are fetched with `curl` + browser UA (can bypass host 403 for bare clients). When using `--full` to fetch full text, it imports `extract_article` from the same repo's `generic`.

## Usage (agent invokes via bash)

```bash
# List current feed entries (newest-first, without landing in mirror):
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-sources/rss/reference/feed.py enum --url <feed-url>

# One-stop: parse + land each entry in mirror + project S1:
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-sources/rss/reference/feed.py sync --url <feed-url> --since 14d

# --full: additionally fetch full article text for each link (most feeds only carry summary):
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-sources/rss/reference/feed.py sync --url <feed-url> --full
```

`--since` accepts `30d`/`12h`/`2026-05-07` (by publication date window; **entries without dates are kept** — no date does not mean old).
`--limit N` truncates the number of entries. `--matrix-entity X` gives S1 a cross-platform same-source tag.

## No single `fetch` subcommand

The content of an RSS entry **is already in the feed** (summary or `content:encoded`), no need to fetch separately. To get **full article text** of a certain entry: ① `sync --full` (fetch full text for each entry in the whole feed), or ② directly use the `generic` `fetch --url` on that entry's link. Responsibilities are cleanly separated: RSS handles feed parsing, arbitrary single-page full-text extraction belongs to `generic`.

## Cursor model (mirror deduplication, no state file)

No persistent cursor file: each `sync` re-parses the entire current feed, **deduplicates against mirror by entry id (guid)**.
RSS feeds only keep a recent window (usually 20–50 entries, old ones roll off), so "fetching all current entries" is bounded and idempotent — same idea as the `--since` window + mirror deduplication for gmail. **Without `--limit`, defaults to fetching all current entries in the feed** (bounded, safe); `--limit` is mainly for capping the first run.

> ⚠️ RSS itself does not support backfill: old entries that have rolled off the feed window cannot be fetched back. For history, you can only rely on the source site's own archive pages (that's `generic`'s job).

## Output schema

```
$AVA_HOME/state/mirrors/rss/<slug>/   # slug = tail segment of link + entry-id short hash (stable)
├── post.json    # platform=rss/url/source_id=guid/feed_url/feed_title/title/text/summary_html/author{id=feed_url,name}/published_at/fetched_*
└── post.md      # rendered version
```

S1 projection: `platform="rss"`, `source_id` = entry guid, `url` = entry link,
**`author.id` = feed URL** (stable follow key, shared by all entries of the feed — analogous to gmail using List-Id as `author.id`), `author.name` = entry author or feed title. `text` by default is the plain text stripped from summary, when `--full` it's replaced with full-text markdown.

## Non-goals / boundaries

- **No feed discovery**: you must provide the exact feed URL. You need to find where a site's RSS is yourself (`<link rel=alternate type=application/rss+xml>` or try `/feed`, `/rss.xml`, `/atom.xml`).
- **`--full` is limited by `generic`**: when the link points to a JS-heavy SPA / paywall, full text extraction may be incomplete, falling back to summary.
- **Zombie feeds are not automatically identified**: some site feeds return 200 but have stopped updating long ago (encountered: People's Daily RSS stopped a year ago). `enum` will faithfully return old entries — judge if it's still alive based on publication dates yourself.

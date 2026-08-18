"""RSS/Atom feed adapter — one self-contained CLI.

The agent runs this as a subprocess (no SDK import, no browser automation), e.g.

    .venv/bin/python skills/web-sources/rss/reference/feed.py enum --url <feed-url>
    .venv/bin/python skills/web-sources/rss/reference/feed.py sync --url <feed-url> --since 14d
    .venv/bin/python skills/web-sources/rss/reference/feed.py sync --url <feed-url> --full

and reads the JSON printed to stdout. A feed IS the collection (no per-account
enumeration), so there are two stages only:

  enum   — fetch + parse the feed, list its current entries newest-first
           (`{id, title, url, published_at}`), bounded by `--since` / `--limit`.
  sync   — same, but persist each entry into the raw mirror
           `$AVA_HOME/state/mirrors/rss/<slug>/` (`post.json` + `post.md`)
           and project to S1. `--full` additionally fetches each entry's link and
           extracts the full article body (most feeds carry only a summary).

There is no per-item `fetch` subcommand: the entry's content already comes inside
the feed, and pulling one arbitrary article's full body is the `generic`
skill's job (which `--full` reuses internally).

Parsing is `feedparser` (handles every RSS/Atom dialect + date normalization);
the feed bytes are pulled with `curl` + a browser UA so a feed host that 403s a
bare client still answers. `--full` imports `generic`'s `fetch_article`, whose
curl -> Jina ladder pulls each entry's full body even past a Cloudflare wall.
Cursor model: no state file — each run re-parses the whole current feed and
dedups against the mirror by entry id (a feed only holds its recent window, so
taking all current entries is bounded and idempotent, like the gmail
`--since`-window mirror dedup).
"""

from __future__ import annotations

import calendar
import datetime as _dt
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

import feedparser
from feedparser.exceptions import ThingsNobodyCaresAboutButMe

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

_RE_TAG = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"[ \t]*\n[ \t]*")


class RssError(Exception):
    """A feed could not be fetched or parsed into any entries."""


# --------------------------------------------------------------------------- #
# Output location
# --------------------------------------------------------------------------- #

_SKILL = "rss-feed"


def _default_root() -> Path:
    """Default raw-mirror dir `$AVA_HOME/state/mirrors/rss/` — shared derived
    state doubling as the sync watermark; the per-content subdir is the stable
    entry slug, so a re-run reuses it (dedup)."""
    home = Path(os.environ.get("AVA_HOME", "~/.ava")).expanduser()
    return home / "state" / "mirrors" / "rss"


def _entry_slug(entry_id: str) -> str:
    """A stable, filesystem-safe directory name for an entry, derived ONLY from
    its id (guid). Keying the dir purely on the guid means a re-run reuses it even
    if the feed's `link` field later changes (canonical redirect, slug edit) — the
    guid is the dedup key, so the dir must not move when the link moves. A readable
    tail (the id's last path segment if it is a URL, else the id text) plus a short
    hash of the id."""
    h = hashlib.sha1(entry_id.encode()).hexdigest()[:10]  # noqa: S324 — dedup id, not security
    parsed = urllib.parse.urlparse(entry_id)
    raw = parsed.path.rstrip("/").rsplit("/", 1)[-1] if parsed.scheme else entry_id
    tail = re.sub(r"[^A-Za-z0-9._-]+", "-", raw)[:40].strip("-")
    return f"{tail or 'entry'}-{h}"


# --------------------------------------------------------------------------- #
# Fetch + parse
# --------------------------------------------------------------------------- #


def _assert_fetchable(url: str) -> None:
    """Reject non-http(s) and obvious internal hosts before curl sees the URL —
    the same defense-in-depth tripwire generic applies (a feed URL is just as
    agent-supplied). Blocks `file://` and a literal loopback/private/link-local
    IP; a hostname resolving to a private IP, or a redirect to one, is not chased
    (curl caps redirects at `--max-redirs`; the agent driving this is trusted)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise RssError(f"refusing non-http(s) feed URL: {url!r}")
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        raise RssError(f"refusing internal host: {host!r}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # a hostname, not a literal IP — allowed
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        raise RssError(f"refusing internal IP: {host!r}")


def _fetch_bytes(url: str) -> bytes:
    """Fetch `url` raw via curl with a browser UA. Raises on HTTP/transport
    failure (`curl -f`)."""
    _assert_fetchable(url)
    proc = subprocess.run(
        ["curl", "-fsSL", "--max-redirs", "5", "--compressed", "--max-time", "45", "-A", _UA, url],
        capture_output=True,
        check=False,  # returncode handled below (curl -f already fails on HTTP errors)
    )
    if proc.returncode != 0:
        raise RssError(
            f"curl failed for {url} (rc={proc.returncode}): {proc.stderr.decode(errors='replace')[:200]}"
        )
    return proc.stdout


def _iso(struct: time.struct_time | None) -> str | None:
    """A feedparser `*_parsed` struct_time (already UTC) -> ISO8601 Z string."""
    if struct is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", struct)


def _html_to_text(html: str) -> str:
    """Strip tags + unescape an RSS summary/content fragment to plain text. The
    feed summary is short prose, so a tag strip (not a full markdown render) is
    enough; `--full` is the path to a real article body."""
    import html as _html

    text = _html.unescape(_RE_TAG.sub("", html))
    return _RE_WS.sub("\n", text).strip()


def _entry_content_html(entry: Any) -> str:
    """The richest body the feed itself carries for `entry`: `content:encoded`
    (full HTML) when present, else the summary."""
    content = entry.get("content")
    if content and content[0].get("value"):
        return content[0]["value"]
    return entry.get("summary", "")


def parse_feed(feed_url: str) -> tuple[str, list[Any]]:
    """Fetch + parse `feed_url`. Returns `(feed_title, entries)`.

    An empty feed (a valid feed dialect with no entries) is not an error — it
    returns an empty list. We raise only when feedparser could not identify the
    content as a known feed dialect (``parsed.version`` is empty), or when a
    *fatal* parse error prevented entry recovery (``bozo`` is set but the
    exception is not a benign ``ThingsNobodyCaresAboutButMe`` like
    ``CharacterEncodingOverride`` — the feed is still valid, just has encoding
    quirks)."""
    parsed = feedparser.parse(_fetch_bytes(feed_url))
    # feedparser's stub types .feed/.entries loosely (FeedParserDict supports both
    # attr and key access); pin to Any so the dict access below is clean.
    feed_meta: Any = parsed.feed
    entries: list[Any] = parsed.entries
    if not entries:
        cause = getattr(parsed, "bozo_exception", None)
        fatal_bozo = parsed.get("bozo") and not isinstance(cause, ThingsNobodyCaresAboutButMe)
        if fatal_bozo or not parsed.get("version"):
            raise RssError(
                f"not recognized as a feed: {feed_url!r}" + (f" ({cause})" if cause else "")
            )
    feed_title: str = feed_meta.get("title") or feed_url
    return feed_title, entries


# --------------------------------------------------------------------------- #
# Enumeration
# --------------------------------------------------------------------------- #


def _entry_meta(entry: Any) -> dict[str, Any]:
    """The id/title/link/published view of an entry (no body)."""
    published = entry.get("published_parsed") or entry.get("updated_parsed")
    link = entry.get("link")
    return {
        "id": entry.get("id") or link or entry.get("title"),
        "title": entry.get("title"),
        "url": link,
        "published_at": _iso(published),
        "_published_struct": published,
    }


def enum(
    feed_url: str, *, since_ts: float | None = None, limit: int | None = None
) -> list[dict[str, Any]]:
    """List a feed's current entries newest-first.

    Each item: `{id, title, url, published_at}`. `--since` drops entries older
    than the cutoff (entries with no date are kept — a missing date is not
    evidence of being old). `--limit` caps the count. Feeds are conventionally
    reverse-chron, but not guaranteed, so the list is sorted by date (undated
    entries kept in feed order at the front)."""
    metas = [_entry_meta(e) for e in feed_entries(feed_url)]
    if since_ts is not None:
        metas = [
            m
            for m in metas
            if m["_published_struct"] is None or calendar.timegm(m["_published_struct"]) >= since_ts
        ]
    # newest-first by date; undated sort as "now" so they stay at the top.
    metas.sort(
        key=lambda m: (
            calendar.timegm(m["_published_struct"]) if m["_published_struct"] else time.time()
        ),
        reverse=True,
    )
    if limit is not None:
        metas = metas[:limit]
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in metas]


def feed_entries(feed_url: str) -> list[Any]:
    """Parse `feed_url` and return its raw feedparser entries (cached per call
    site by the caller if needed)."""
    _title, entries = parse_feed(feed_url)
    return entries


# --------------------------------------------------------------------------- #
# Per-entry persist -> raw mirror
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.UTC).isoformat().replace("+00:00", "Z")


def _webscrape() -> Any:
    """Load the sibling `generic` skill's module (for `--full` body
    extraction). Same cross-skill load pattern the other media adapters use for
    audio-transcribe."""
    mod_path = Path(__file__).parents[2] / "generic" / "reference" / "feed.py"
    spec = importlib.util.spec_from_file_location("web_scrape_feed", mod_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load generic skill at {mod_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_post(feed_url: str, feed_title: str, entry: Any, *, full: bool = False) -> dict[str, Any]:
    """Build the curated post dict for one feed entry.

    Body is the feed-provided summary/content stripped to text; with `full=True`
    the entry's link is fetched and its full article body (markdown) replaces it.
    `author.id` is the feed URL — the stable follow key (entries share it), the
    way the gmail adapter keys on List-Id."""
    meta = _entry_meta(entry)
    summary_html = _entry_content_html(entry)
    text = _html_to_text(summary_html)
    got_full = False
    if full and meta["url"]:
        ws = _webscrape()
        # `fetch_article` runs the curl -> Jina ladder, so a Cloudflare-fronted
        # host (which 403s a bare curl) still yields full text via the render rung.
        # Only when BOTH rungs fail (a login-gated page) does this raise: swallow
        # it, fall back to the feed summary, and leave `_full` False so a later
        # --full run retries this one. The whole feed sync must not abort here.
        try:
            art, _via = ws.fetch_article(meta["url"])
        except ws.WebScrapeError as e:
            print(f"[full-fallback] {meta['url']}: {e}", file=sys.stderr)
        else:
            if art["text"].strip():
                text = art["text"]
                got_full = True
    return {
        "platform": "rss",
        "url": meta["url"],
        "source_id": meta["id"],
        "feed_url": feed_url,
        "feed_title": feed_title,
        "title": meta["title"],
        "text": text,
        "summary_html": summary_html,
        "author": {"id": feed_url, "name": entry.get("author") or feed_title},
        "published_at": meta["published_at"],
        "fetched_at": _now_iso(),
        "fetched_via": _SKILL,
        "_full": got_full,
    }


def save(post: dict[str, Any], root: Path | None = None) -> Path:
    """Persist a post to `$AVA_HOME/state/mirrors/rss/<slug>/`."""
    outdir = (root or _default_root()) / _entry_slug(post["source_id"])
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "post.json").write_text(json.dumps(post, ensure_ascii=False, indent=2))
    (outdir / "post.md").write_text(_render_markdown(post))
    return outdir


def _render_markdown(post: dict[str, Any]) -> str:
    lines = [
        f"# {post.get('title') or post['source_id']}",
        "",
        f"- **feed**: {post.get('feed_title')} ({post.get('feed_url')})",
        f"- **author**: {post['author'].get('name')}",
        f"- **url**: {post.get('url')}",
        f"- **published**: {post.get('published_at')}",
        "",
        post.get("text") or "_(no body)_",
    ]
    return "\n".join(lines) + "\n"


def to_s1(
    post: dict[str, Any], *, matrix_entity: str | None = None, root: Path | None = None
) -> dict[str, Any]:
    """Project a post onto the cross-platform S1 item schema shared by every
    `web-sources` adapter (see `web-sources/SKILL.md`). `root` must match the dir
    the post was saved under so `raw_path` points at the real mirror."""
    a = post["author"]
    return {
        "platform": "rss",
        "source_id": post["source_id"],
        "url": post.get("url"),
        "author": {"id": a.get("id"), "name": a.get("name")},
        "matrix_entity": matrix_entity,
        "title": post.get("title"),
        "text": post.get("text"),
        "transcript": None,
        "published_at": post.get("published_at"),
        "fetched_at": post.get("fetched_at"),
        "fetched_via": post.get("fetched_via"),
        "media": [],
        "stats": {},
        "raw_path": str((root or _default_root()) / _entry_slug(post["source_id"])) + "/",
    }


def _load_if_complete(entry_id: str, *, full: bool, root: Path | None) -> dict[str, Any] | None:
    """Return the saved post for an entry if its mirror already has everything
    this run would produce (so a re-run reuses it), else None. A `--full` run
    requires the saved post to actually carry a full body, not just a summary."""
    outdir = (root or _default_root()) / _entry_slug(entry_id)
    post_json = outdir / "post.json"
    if not post_json.exists():
        return None
    post: dict[str, Any] = json.loads(post_json.read_text())
    if full and not post.get("_full"):
        return None
    return post


def sync(
    feed_url: str,
    *,
    since_ts: float | None = None,
    limit: int | None = None,
    full: bool = False,
    matrix_entity: str | None = None,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """Parse a feed, persist each current entry, return the S1 items.

    Entries already mirrored are reused from disk. Without `--limit` every entry
    currently in the feed is taken (a feed holds only its recent window, so this
    is bounded) — `--since` windows by publish date. `--limit` caps the number of
    *newly built* entries (reused ones don't count), so a re-run advances through
    the backlog instead of re-examining the same leading N — without this, a
    `--limit` smaller than the count of new entries would strand the rest before
    they roll off the feed."""
    feed_title, entries = parse_feed(feed_url)
    metas = [(e, _entry_meta(e)) for e in entries]
    if since_ts is not None:
        metas = [
            (e, m)
            for (e, m) in metas
            if m["_published_struct"] is None or calendar.timegm(m["_published_struct"]) >= since_ts
        ]
    metas.sort(
        key=lambda em: (
            calendar.timegm(em[1]["_published_struct"])
            if em[1]["_published_struct"]
            else time.time()
        ),
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    made = 0
    for entry, meta in metas:
        post = _load_if_complete(meta["id"], full=full, root=root)
        if post is None:
            if limit is not None and made >= limit:
                break  # NEW-item cap reached; the rest wait for the next run
            post = build_post(feed_url, feed_title, entry, full=full)
            save(post, root=root)
            made += 1
        out.append(to_s1(post, matrix_entity=matrix_entity, root=root))
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _since_to_ts(since: str | None) -> float | None:
    """Parse a `--since` value to a unix-seconds cutoff: `30d` / `12h` / an ISO
    date `2026-05-07`."""
    if since is None:
        return None
    m = re.fullmatch(r"(\d+)([dh])", since)
    if m:
        secs = int(m.group(1)) * (86400 if m.group(2) == "d" else 3600)
        return time.time() - secs
    parsed = _dt.datetime.fromisoformat(since)
    parsed = parsed.replace(tzinfo=_dt.UTC) if parsed.tzinfo is None else parsed.astimezone(_dt.UTC)
    return parsed.timestamp()


def _main(argv: list[str] | None = None) -> None:
    import argparse

    p = argparse.ArgumentParser(
        prog="rss-feed",
        description="enumerate / sync an RSS/Atom feed -> raw mirror + S1 JSON",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("enum", help="list a feed's current entries")
    pe.add_argument("--url", required=True, help="the feed URL")
    pe.add_argument("--since", help="time bound: 30d / 12h / 2026-05-07")
    pe.add_argument("--limit", type=int, help="max entries")

    ps = sub.add_parser("sync", help="sync a feed's entries into the mirror + S1")
    ps.add_argument("--url", required=True, help="the feed URL")
    ps.add_argument("--since", help="time bound: 30d / 12h / 2026-05-07")
    ps.add_argument("--limit", type=int, help="max entries")
    ps.add_argument(
        "--full", action="store_true", help="fetch each entry's link for the full article body"
    )
    ps.add_argument("--matrix-entity", help="cross-platform entity tag for S1")
    ps.add_argument("--out", help="raw-mirror dir (default $AVA_HOME/state/mirrors/rss)")

    args = p.parse_args(argv)
    if args.cmd == "enum":
        result: Any = enum(args.url, since_ts=_since_to_ts(args.since), limit=args.limit)
    else:
        result = sync(
            args.url,
            since_ts=_since_to_ts(args.since),
            limit=args.limit,
            full=args.full,
            matrix_entity=args.matrix_entity,
            root=Path(args.out).expanduser() if args.out else None,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()

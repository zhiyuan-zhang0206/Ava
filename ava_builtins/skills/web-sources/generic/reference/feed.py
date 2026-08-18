"""Web-scrape adapter — one self-contained CLI for arbitrary web pages.

The agent runs this as a subprocess (no SDK import, no browser automation), e.g.

    .venv/bin/python skills/web-sources/generic/reference/feed.py fetch --url https://site/article
    .venv/bin/python skills/web-sources/generic/reference/feed.py enum --url https://site/list --link-pattern 'mod=view&aid=\\d+'
    .venv/bin/python skills/web-sources/generic/reference/feed.py sync --url https://site/list --link-pattern '...' --limit 20

and reads the JSON printed to stdout. This is the generic fallback adapter for
sources with no platform API and no RSS feed (official newspaper sites, forum
portals, blog index pages). Two stages:

  enum   — fetch a list/index page and harvest the article links on it whose
           absolute URL matches a caller-supplied regex (`--link-pattern`).
           The pattern is how the agent says "these anchors are articles" — a
           general crawler can't guess that per-site, so it's an explicit knob.
           Runs the same curl -> Jina ladder as `fetch`, so a list page that
           403s a bare client still enumerates (the Jina rung returns markdown,
           harvested via its `[text](url)` links).
  fetch  — fetch one page, extract the main article (boilerplate stripped) to
           markdown + metadata (title / author / date / site) into the raw
           mirror `$AVA_HOME/state/mirrors/web-scrape/<slug>/`, projected to S1.

`sync` ties them: harvest the list -> fetch each new one -> print the S1 items.

The HTTP fetch is a two-rung ladder. Rung 1 is `curl` with an honest browser
User-Agent (many sites, Chinese official/news especially, 403 a bare client or
time out the WebFetch tool but answer a browser UA) + `trafilatura` extraction
(main-content detection + HTML->markdown + metadata). When that trips an anti-bot
wall (a Cloudflare challenge, a JS-render shell), rung 2 escalates to Jina Reader
(`r.jina.ai`), a server-side headless browser that renders past the wall. A page
that even a render can't read (login-gated) raises, pointing at a logged-in
browser (chrome MCP) or the matching platform adapter. `curl` + `trafilatura` are
the only third-party pieces; Jina is a plain HTTP call.

`fetch_article` is the shared laddered primitive the `rss` adapter imports for
its `--full` mode (pulling an entry's full body from its link). A heavy SPA whose
content needs a real interactive browser is out of scope here.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any

import trafilatura
from loguru import logger

# An honest browser UA: a bare client UA is 403'd / timed out by many news and
# forum sites that answer a normal browser request (verified against Chinese
# official / forum sites that 403 the WebFetch tool but 200 a Chrome UA via curl).
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

# href="..." / href='...' on a list page; absolute-resolved + pattern-filtered.
_RE_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

# Markdown `[text](url)` links on a Jina-rendered list page — the enum escalation
# rung returns markdown (see `_fetch_list_page`), so link harvesting runs over
# these in place of `href="..."`; Jina emits absolute URLs, urljoin is a no-op
# safety net.
_RE_MD_LINK = re.compile(r"""\[[^\]]*\]\(([^)\s]+)\)""")

# Jina Reader: a server-side fetch that runs a real headless browser + JS render
# and returns the page as clean markdown. The escalation rung above plain curl
# for sources that gate a bare client behind an anti-bot / JS-render wall
# (Cloudflare challenge, "enable JavaScript" shell). Anonymous works for much of
# the open web; a JINA_API_KEY (env) adds proxy rotation + forced render for the
# harder hosts and lifts the rate limit.
_JINA_BASE = "https://r.jina.ai/"

# Substrings that mean the bytes we got are an anti-bot / JS-render wall, not the
# article — checked against both the raw HTML and the extracted body. Tripping one
# (or a curl transport failure) is what escalates the fetch ladder to Jina.
_WALL_MARKERS = (
    "just a moment",
    "enable javascript and cookies",
    "checking your browser",
    "verify you are human",
    "attention required",
    "security verification",
    "please complete security verification",
    "please wait",
)


class WebScrapeError(Exception):
    """A page could not be fetched (bad scheme, blocked host, curl failure) or
    yielded no extractable article body."""


# --------------------------------------------------------------------------- #
# Output location
# --------------------------------------------------------------------------- #

_SKILL = "web-scrape"


def _default_root() -> Path:
    """Default raw-mirror dir `$AVA_HOME/state/mirrors/web-scrape/` — shared derived
    state doubling as the sync watermark; the per-content subdir is the stable
    URL slug, so a re-run reuses it (dedup)."""
    home = Path(os.environ.get("AVA_HOME", "~/.ava")).expanduser()
    return home / "state" / "mirrors" / "web-scrape"


def _url_slug(url: str) -> str:
    """A stable, filesystem-safe directory name for `url`. Deterministic (so a
    re-run of the same URL reuses its mirror dir): a readable tail from the path
    plus a short hash of the full URL for uniqueness."""
    h = hashlib.sha1(url.encode()).hexdigest()[:10]  # noqa: S324 — dedup id, not security
    parsed = urllib.parse.urlparse(url)
    tail = parsed.path.rstrip("/").rsplit("/", 1)[-1] or parsed.netloc
    tail = re.sub(r"[^A-Za-z0-9._-]+", "-", tail)[:40].strip("-") or parsed.netloc.replace(".", "-")
    return f"{tail}-{h}"


# --------------------------------------------------------------------------- #
# HTTP fetch (curl + browser UA)
# --------------------------------------------------------------------------- #


def _assert_fetchable(url: str) -> None:
    """Reject non-http(s) and obvious internal hosts before curl sees the URL.

    Defense-in-depth for an agent-supplied URL: blocks `file://` and a literal
    loopback/private/link-local IP. A hostname that resolves to a private IP is
    not chased here (would need a resolve) — the agent driving this is trusted;
    this is a tripwire, not a sandbox."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise WebScrapeError(f"refusing non-http(s) URL: {url!r}")
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        raise WebScrapeError(f"refusing internal host: {host!r}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # a hostname, not a literal IP — allowed
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        raise WebScrapeError(f"refusing internal IP: {host!r}")


def _fetch_html(url: str) -> str:
    """Fetch `url` as text via curl with a browser UA. Raises on HTTP error
    (`curl -f`) or transport failure."""
    _assert_fetchable(url)
    proc = subprocess.run(
        ["curl", "-fsSL", "--max-redirs", "5", "--compressed", "--max-time", "45", "-A", _UA, url],
        capture_output=True,
        check=False,  # returncode handled below (curl -f already fails on HTTP errors)
    )
    if proc.returncode != 0:
        raise WebScrapeError(
            f"curl failed for {url} (rc={proc.returncode}): {proc.stderr.decode(errors='replace')[:200]}"
        )
    return proc.stdout.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Article extraction (shared with rss --full)
# --------------------------------------------------------------------------- #


def extract_article(html: str, url: str) -> dict[str, Any]:
    """Extract the main article from `html` into markdown + metadata.

    Returns `{title, author, date, sitename, hostname, description, text}` where
    `text` is the boilerplate-stripped body as markdown (empty string if the
    extractor found no main content — e.g. a pure index page). This is the
    primitive `rss --full` reuses to pull an entry's full body."""
    body = trafilatura.extract(
        html, url=url, output_format="markdown", include_links=True, include_images=True
    )
    meta = trafilatura.extract_metadata(html, default_url=url)
    host = urllib.parse.urlparse(url).hostname
    return {
        "title": getattr(meta, "title", None),
        "author": getattr(meta, "author", None),
        "date": getattr(meta, "date", None),
        "sitename": getattr(meta, "sitename", None),
        "hostname": getattr(meta, "hostname", None) or host,
        "description": getattr(meta, "description", None),
        "text": body or "",
    }


def _has_wall_marker(text: str) -> bool:
    """True if `text` looks like an anti-bot / JS-render wall rather than content."""
    head = text[:1000].lower()
    return any(marker in head for marker in _WALL_MARKERS)


def _split_jina(raw: str) -> tuple[str | None, str]:
    """Split a Jina Reader response into `(title, markdown_body)`. Jina prefixes
    `Title:` / `URL Source:` lines and a `Markdown Content:` divider before the
    body; if the divider is absent the whole response is treated as the body."""
    m = re.search(r"^Title:\s*(.+)$", raw, re.MULTILINE)
    title = m.group(1).strip() if m else None
    i = raw.find("Markdown Content:")
    body = raw[i + len("Markdown Content:") :] if i != -1 else raw
    return title, body.strip()


def _jina_render(url: str) -> str:
    """Render `url` through Jina Reader (server-side headless browser) and return
    the raw markdown response. The shared escalation rung for both article bodies
    (`_jina_article`) and list pages (`_fetch_list_page`); anonymous works for
    much of the open web, JINA_API_KEY (env) adds proxy rotation + forced render.

    Raises:
        WebScrapeError: the render call failed, or the target itself refused the
            render (Jina echoes e.g. "Target URL returned error 403: Forbidden").
    """
    _assert_fetchable(url)
    args = ["curl", "-fsSL", "--max-time", "60", "-A", _UA]
    key = os.environ.get("JINA_API_KEY")
    if key:
        args += ["-H", f"Authorization: Bearer {key}"]
    args.append(_JINA_BASE + urllib.parse.quote(url, safe=""))
    proc = subprocess.run(args, capture_output=True, check=False)
    if proc.returncode != 0:
        raise WebScrapeError(
            f"jina render failed for {url} (rc={proc.returncode}): "
            f"{proc.stderr.decode(errors='replace')[:200]}"
        )
    raw = proc.stdout.decode("utf-8", errors="replace")
    if "Target URL returned error" in raw:
        raise WebScrapeError(
            f"jina render of {url}: the target refused the render "
            f"({raw.splitlines()[0] if raw else 'no response'})"
        )
    return raw


def _jina_article(url: str) -> dict[str, Any]:
    """Render `url` through Jina Reader (server-side headless browser) into the
    same shape as `extract_article`. The escalation rung when curl is walled.

    Raises:
        WebScrapeError: the render call failed, or it too came back empty / walled
            (the page is likely login-gated — needs a logged-in browser).
    """
    title, body = _split_jina(_jina_render(url))
    if not body or _has_wall_marker(body):
        raise WebScrapeError(
            f"no body from {url} via curl or jina render — the page is likely "
            "login-gated; use a logged-in browser (chrome MCP) or the platform adapter"
        )
    return {
        "title": title,
        "author": None,
        "date": None,
        "sitename": None,
        "hostname": urllib.parse.urlparse(url).hostname,
        "description": None,
        "text": body,
    }


def fetch_article(url: str) -> tuple[dict[str, Any], str]:
    """Fetch one URL's article body, escalating past anti-bot walls. Returns
    `(article, fetched_via)` where `article` matches `extract_article`'s shape.

    Ladder: curl + trafilatura first; if that trips a wall marker (Cloudflare
    challenge, JS shell) or curl is blocked outright, escalate to a Jina render.
    A plain index/login page with no wall signature is NOT escalated — it raises
    so the caller switches to `enum --link-pattern`. This is the primitive both
    `fetch` and `rss --full` use.

    Raises:
        WebScrapeError: no extractable body via either rung (index page, or a
            login-gated page that even a render can't read).
    """
    html = ""
    art: dict[str, Any] | None = None
    try:
        html = _fetch_html(url)
        art = extract_article(html, url)
    except WebScrapeError as e:
        logger.warning("WebScrapeError while fetching article html for {}: {}", url, e)
    body = (art["text"].strip() if art else "").strip()
    walled = _has_wall_marker(html) or (bool(body) and _has_wall_marker(body))
    if art is not None and body and not walled:
        return art, _SKILL
    if art is not None and not body and not walled:
        # transport worked but there is no article and no wall signature: an index
        # or login page, not something a render fixes — tell the caller plainly.
        raise WebScrapeError(
            f"no article body extracted from {url} "
            "(is it a list/index page? use enum --link-pattern, or it may be a login wall)"
        )
    return _jina_article(url), f"{_SKILL}+jina"


# --------------------------------------------------------------------------- #
# Per-item fetch -> raw mirror
# --------------------------------------------------------------------------- #


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.UTC).isoformat().replace("+00:00", "Z")


def fetch(url: str, *, root: Path | None = None, platform: str = "web") -> dict[str, Any]:
    """Fetch one web page into the raw mirror `$AVA_HOME/state/mirrors/web-scrape/<slug>/`.

    Writes `post.json` + `post.md` and returns the curated post dict. Runs the
    curl -> Jina fetch ladder (see `fetch_article`); `fetched_via` records which
    rung produced the body (`web-scrape` or `web-scrape+jina`). `platform` tags
    the S1 item (default `web`); locally-installed adapters pass their own value
    so downstream consumers can tell which adapter produced the item.
    Raises if the page yields no extractable body on either rung (a list/index
    page fetched as an article, or a login wall) — a real "nothing to ingest",
    not a silent empty."""
    art, via = fetch_article(url)
    post: dict[str, Any] = {
        "platform": platform,
        "url": url,
        "source_id": url,
        "title": art["title"],
        "text": art["text"],
        "description": art["description"],
        "author": {
            "id": art["hostname"],
            "name": art["author"] or art["sitename"] or art["hostname"],
        },
        "sitename": art["sitename"],
        "published_at": art["date"],
        "fetched_at": _now_iso(),
        "fetched_via": via,
    }
    save(post, root=root)
    return post


def save(post: dict[str, Any], root: Path | None = None) -> Path:
    """Persist a fetched post to `$AVA_HOME/state/mirrors/web-scrape/<slug>/`."""
    outdir = (root or _default_root()) / _url_slug(post["url"])
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "post.json").write_text(json.dumps(post, ensure_ascii=False, indent=2))
    (outdir / "post.md").write_text(_render_markdown(post))
    return outdir


def _render_markdown(post: dict[str, Any]) -> str:
    a = post["author"]
    lines = [
        f"# {post.get('title') or post['url']}",
        "",
        f"- **site**: {post.get('sitename')} ({a.get('id')})",
        f"- **author**: {a.get('name')}",
        f"- **url**: {post['url']}",
        f"- **published**: {post.get('published_at')}",
        "",
        post.get("text") or "_(no body)_",
    ]
    return "\n".join(lines) + "\n"


def to_s1(
    post: dict[str, Any], *, matrix_entity: str | None = None, root: Path | None = None
) -> dict[str, Any]:
    """Project a fetched post onto the cross-platform S1 item schema shared by every
    `web-sources` adapter (see `web-sources/SKILL.md`). `root` must match the dir
    the post was saved under so `raw_path` points at the real mirror."""
    a = post["author"]
    return {
        "platform": post.get("platform", "web"),
        "source_id": post["url"],
        "url": post["url"],
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
        "raw_path": str((root or _default_root()) / _url_slug(post["url"])) + "/",
    }


def _load_if_complete(url: str, *, root: Path | None) -> dict[str, Any] | None:
    """Return the saved post for `url` if its mirror already has `post.json` (so a
    re-run reuses it instead of re-fetching), else None."""
    post_json = (root or _default_root()) / _url_slug(url) / "post.json"
    if not post_json.exists():
        return None
    return json.loads(post_json.read_text())


# --------------------------------------------------------------------------- #
# Enumeration (harvest article links off a list page)
# --------------------------------------------------------------------------- #


def _fetch_list_page(url: str) -> tuple[str, str, str]:
    """Fetch a list/index page for link harvesting, escalating past anti-bot
    walls exactly like `fetch_article` does for article bodies.

    Returns `(content, fmt, via)`: rung 1 is curl + a browser UA (HTML); if curl
    is blocked (403/timeout) or returns a wall shell (Cloudflare challenge, JS
    shell), rung 2 is a Jina render whose markdown comes back instead (`fmt` is
    then `"markdown"`, and `via` records `web-scrape+jina`). A plain page with no
    wall signature is NOT escalated — an empty harvest is a pattern problem, not
    something a render fixes.
    """
    content = ""
    via = _SKILL
    try:
        content = _fetch_html(url)
    except WebScrapeError as e:
        logger.warning("enum: curl failed for {}: {}", url, e)
    if not content or _has_wall_marker(content):
        content = _jina_render(url)
        via = f"{_SKILL}+jina"
    return content, ("html" if via == _SKILL else "markdown"), via


def _iter_hrefs(content: str, fmt: str) -> list[str]:
    """Raw hrefs on a list page: `href="..."` from HTML (curl rung) or markdown
    `[text](url)` links from a Jina render (the enum escalation rung)."""
    return _RE_MD_LINK.findall(content) if fmt == "markdown" else _RE_HREF.findall(content)


def harvest_links(
    list_url: str, link_pattern: str | None, *, limit: int | None = None
) -> list[str]:
    """Fetch `list_url` and return the absolute article links on it.

    Every `href` is resolved against `list_url` and (if `link_pattern` is given)
    kept only when its absolute URL matches the regex. Order is preserved and
    duplicates dropped, so the first `limit` are the page's first N matching
    links (list pages are newest-first, so that is the newest N).

    Runs the same two-rung fetch ladder as `fetch_article`: curl + browser UA
    first, then a Jina render when the list page is blocked (403/timeout) or
    walled — a list page that 403s a bare client still enumerates. The Jina rung
    returns markdown, so links are harvested from its `[text](url)` links.
    """
    import html as _html

    page, fmt, via = _fetch_list_page(list_url)
    pat = re.compile(link_pattern) if link_pattern else None
    seen: set[str] = set()
    out: list[str] = []
    for href in _iter_hrefs(page, fmt):
        # Unescape HTML entities first: an href written `?mod=view&amp;aid=1`
        # carries the literal `&amp;`, which would defeat a `&aid=` pattern and
        # silently drop the link. Markdown URLs are already plain.
        raw = _html.unescape(href) if fmt == "html" else href
        absu = urllib.parse.urljoin(list_url, raw).split("#", 1)[0]
        if pat and not pat.search(absu):
            continue
        if absu in seen:
            continue
        seen.add(absu)
        out.append(absu)
        if limit is not None and len(out) >= limit:
            break
    logger.info("enum {}: {} link(s) via {}", list_url, len(out), via)
    return out


def sync(
    list_url: str,
    *,
    link_pattern: str | None,
    limit: int | None = None,
    matrix_entity: str | None = None,
    root: Path | None = None,
    platform: str = "web",
) -> list[dict[str, Any]]:
    """Harvest a list page's article links, fetch each, return the S1 items.

    Pages whose mirror already has `post.json` are reused from disk (no
    re-fetch) — re-running the same list page is the natural dedup for a repeated
    digest. A page that yields no body is skipped (with its error noted on
    stderr), not fatal, so one dead link doesn't abort the whole sync.

    `--limit` caps the number of *newly fetched* articles (reused ones don't
    count), so a re-run advances through the backlog instead of re-examining the
    same leading N. If links were harvested but none yielded an article, that is
    a real failure (bad `--link-pattern`, a login wall) -> raise, not a silent
    empty result. `platform` tags the S1 items (default `web`); locally-installed
    adapters pass their own value so downstream consumers can tell which adapter
    produced the item."""
    import sys

    urls = harvest_links(list_url, link_pattern)
    if not urls:
        return []  # genuinely no links matched -> a valid empty window
    out: list[dict[str, Any]] = []
    made = 0
    for u in urls:
        post = _load_if_complete(u, root=root)
        if post is None:
            if limit is not None and made >= limit:
                break  # NEW-item cap reached; the rest wait for the next run
            try:
                post = fetch(u, root=root, platform=platform)
            except WebScrapeError as e:
                print(f"[skip] {u}: {e}", file=sys.stderr)
                continue
            made += 1
        out.append(to_s1(post, matrix_entity=matrix_entity, root=root))
    if not out:
        raise WebScrapeError(
            f"harvested {len(urls)} link(s) from {list_url} but none yielded an article "
            "— check --link-pattern (is it matching real article pages?) or a login wall"
        )
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _main(argv: list[str] | None = None) -> None:
    import argparse

    p = argparse.ArgumentParser(
        prog="web-scrape",
        description="fetch / enumerate arbitrary web pages -> raw mirror + S1 JSON",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("enum", help="harvest article links off a list/index page")
    pe.add_argument("--url", required=True, help="the list/index page URL")
    pe.add_argument(
        "--link-pattern", help="regex an absolute href must match to count as an article link"
    )
    pe.add_argument("--limit", type=int, help="max links")

    ps = sub.add_parser("sync", help="harvest a list page + fetch each article")
    ps.add_argument("--url", required=True, help="the list/index page URL")
    ps.add_argument("--link-pattern", help="regex an absolute href must match")
    ps.add_argument("--limit", type=int, help="max articles")
    ps.add_argument("--matrix-entity", help="cross-platform entity tag for S1")
    ps.add_argument("--out", help="raw-mirror dir (default $AVA_HOME/state/mirrors/web-scrape)")

    pf = sub.add_parser("fetch", help="fetch one page as an article")
    pf.add_argument("--url", required=True, help="the article URL")
    pf.add_argument("--matrix-entity", help="cross-platform entity tag for S1")
    pf.add_argument("--out", help="raw-mirror dir (default $AVA_HOME/state/mirrors/web-scrape)")

    args = p.parse_args(argv)
    if args.cmd == "enum":
        result: Any = harvest_links(args.url, args.link_pattern, limit=args.limit)
    elif args.cmd == "sync":
        result = sync(
            args.url,
            link_pattern=args.link_pattern,
            limit=args.limit,
            matrix_entity=args.matrix_entity,
            root=Path(args.out).expanduser() if args.out else None,
        )
    else:
        root = Path(args.out).expanduser() if args.out else None
        result = to_s1(fetch(args.url, root=root), matrix_entity=args.matrix_entity, root=root)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()

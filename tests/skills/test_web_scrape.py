"""Hermetic unit tests for the web-sources:generic skill (ava_builtins/skills/web-sources/generic/reference/feed.py).

The skill's live behavior (curl against real 403-prone sites + trafilatura main-
content extraction) was verified by hand during the build; these lock the *pure*
logic that regresses silently — the URL slug stability, the SSRF guard, the
link-pattern harvest, real trafilatura extraction on fixture HTML, the fetch ->
mirror -> S1 path, the mirror dedup, and the per-cluster output root — by mocking
only the HTTP fetch so nothing hits the network.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_PATH = (
    Path(__file__).parents[2]
    / "ava_builtins"
    / "skills"
    / "web-sources"
    / "generic"
    / "reference"
    / "feed.py"
)
_spec = importlib.util.spec_from_file_location("web_scrape_under_test", _PATH)
assert _spec and _spec.loader
feed = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = feed
_spec.loader.exec_module(feed)


_ARTICLE_HTML = """<html><head><title>Headline Here</title>
<meta property="og:site_name" content="Example News"/>
<meta property="article:published_time" content="2026-06-08T10:00:00Z"/>
<meta name="author" content="Jane Doe"/></head>
<body><article><h1>Headline Here</h1>
<p>First paragraph of the body with enough words to be considered the main
content by the extractor algorithm running over this fixture page here.</p>
<p>Second paragraph also carries a fair amount of text so the extractor keeps it
in the extracted markdown output reliably across versions.</p>
</article><nav class="menu">home about contact junk to drop</nav></body></html>"""

_LIST_HTML = """<html><body>
<a href="/portal.php?mod=view&aid=101">Article 101</a>
<a href="portal.php?mod=view&aid=102">Article 102</a>
<a href="https://other.example.com/portal.php?mod=view&aid=103">Cross-site 103</a>
<a href="/portal.php?mod=view&aid=101#comments">Dup 101 with fragment</a>
<a href="/about.html">Not an article</a>
<a href="https://ad.example.com/banner">Ad</a>
</body></html>"""


# --------------------------------------------------------------------------- #
# URL slug
# --------------------------------------------------------------------------- #


def test_url_slug_is_stable_and_safe() -> None:
    url = "https://forum.example/portal.php?mod=view&aid=51906"
    s1 = feed._url_slug(url)
    s2 = feed._url_slug(url)
    assert s1 == s2  # deterministic -> a re-run reuses the same mirror dir
    assert "/" not in s1 and "?" not in s1 and "&" not in s1
    assert feed._url_slug(url + "x") != s1  # different URL -> different dir


# --------------------------------------------------------------------------- #
# SSRF guard
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "http://localhost/admin",
        "http://127.0.0.1/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
    ],
)
def test_assert_fetchable_rejects(url: str) -> None:
    with pytest.raises(feed.WebScrapeError):
        feed._assert_fetchable(url)


@pytest.mark.parametrize("url", ["https://example.com/a", "http://news.site.cn/x.html"])
def test_assert_fetchable_allows(url: str) -> None:
    feed._assert_fetchable(url)  # no raise


# --------------------------------------------------------------------------- #
# Link harvest
# --------------------------------------------------------------------------- #


def test_harvest_links_pattern_absolute_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_html(_url: str) -> str:
        return _LIST_HTML

    monkeypatch.setattr(feed, "_fetch_html", fake_html)
    links = feed.harvest_links("https://forum.example/portal.php", r"mod=view&aid=\d+")
    # relative resolved to absolute; fragment stripped collapses the dup; the
    # cross-site and non-article links are kept/dropped purely by the pattern.
    assert links == [
        "https://forum.example/portal.php?mod=view&aid=101",
        "https://forum.example/portal.php?mod=view&aid=102",
        "https://other.example.com/portal.php?mod=view&aid=103",
    ]


def test_harvest_links_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_html(_url: str) -> str:
        return _LIST_HTML

    monkeypatch.setattr(feed, "_fetch_html", fake_html)
    links = feed.harvest_links("https://forum.example/portal.php", r"mod=view&aid=\d+", limit=2)
    assert len(links) == 2


def test_harvest_links_unescapes_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    # an href written with `&amp;` must still match a `&aid=` pattern (the entity
    # is decoded before the regex), otherwise the link is silently dropped.
    def fake_html(_url: str) -> str:
        return '<a href="/portal.php?mod=view&amp;aid=7">x</a>'

    monkeypatch.setattr(feed, "_fetch_html", fake_html)
    links = feed.harvest_links("https://s.cn/portal.php", r"mod=view&aid=\d+")
    assert links == ["https://s.cn/portal.php?mod=view&aid=7"]


# --------------------------------------------------------------------------- #
# Extraction (real trafilatura on fixture HTML)
# --------------------------------------------------------------------------- #


def test_extract_article_real() -> None:
    art = feed.extract_article(_ARTICLE_HTML, "https://example.com/news/headline")
    assert art["title"] == "Headline Here"
    assert art["author"] == "Jane Doe"
    assert art["date"] == "2026-06-08"
    assert art["sitename"] == "Example News"
    assert "First paragraph of the body" in art["text"]
    assert "junk to drop" not in art["text"]  # nav boilerplate stripped


# --------------------------------------------------------------------------- #
# fetch -> mirror -> S1
# --------------------------------------------------------------------------- #


def test_fetch_writes_mirror_and_s1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    url = "https://example.com/news/headline"

    def fake_html(_url: str) -> str:
        return _ARTICLE_HTML

    monkeypatch.setattr(feed, "_fetch_html", fake_html)
    post = feed.fetch(url, root=tmp_path)
    outdir = tmp_path / feed._url_slug(url)
    assert (outdir / "post.json").exists()
    assert (outdir / "post.md").exists()
    assert post["author"]["id"] == "example.com"

    s1 = feed.to_s1(post, root=tmp_path)
    assert s1["platform"] == "web"
    assert s1["source_id"] == url
    assert s1["author"]["id"] == "example.com"
    assert s1["raw_path"] == str(outdir) + "/"


def test_fetch_no_body_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # trafilatura is recall-oriented (it extracts even small nav text), so the
    # guard only fires on a genuinely empty extraction — mock that directly
    # rather than depend on the extractor's threshold for a specific fixture.
    def fake_html(_url: str) -> str:
        return "<html><body></body></html>"

    def empty_extract(_html: str, _url: str) -> dict[str, Any]:
        return {
            "title": None,
            "author": None,
            "date": None,
            "sitename": None,
            "hostname": "example.com",
            "description": None,
            "text": "",
        }

    monkeypatch.setattr(feed, "_fetch_html", fake_html)
    monkeypatch.setattr(feed, "extract_article", empty_extract)
    with pytest.raises(feed.WebScrapeError, match="no article body"):
        feed.fetch("https://example.com/list", root=tmp_path)


def test_sync_dedups_and_skips_dead(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    good = "https://example.com/portal.php?mod=view&aid=101"
    dead = "https://example.com/portal.php?mod=view&aid=102"
    calls: list[str] = []

    def fake_html(url: str) -> str:
        calls.append(url)
        if url.endswith("list"):
            return f'<a href="{good}">a</a><a href="{dead}">b</a>'
        return _ARTICLE_HTML

    def selective_extract(_html: str, url: str) -> dict[str, Any]:
        text = "Real body content here." if url == good else ""  # dead -> empty -> skipped
        return {
            "title": "T",
            "author": None,
            "date": None,
            "sitename": None,
            "hostname": "example.com",
            "description": None,
            "text": text,
        }

    monkeypatch.setattr(feed, "_fetch_html", fake_html)
    monkeypatch.setattr(feed, "extract_article", selective_extract)
    items = feed.sync("https://example.com/list", link_pattern=r"aid=\d+", root=tmp_path)
    assert [i["url"] for i in items] == [good]  # dead link skipped, not fatal

    # second run reuses the good article's mirror (no re-fetch of it)
    calls.clear()
    feed.sync("https://example.com/list", link_pattern=r"aid=\d+", root=tmp_path)
    assert good not in calls  # loaded from disk, not re-fetched


def test_sync_all_links_fail_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # links harvested but none extractable -> raise (bad pattern / wall), not a
    # silent empty success.
    def fake_html(url: str) -> str:
        if url.endswith("list"):
            return '<a href="https://e.com/x?aid=1">a</a><a href="https://e.com/y?aid=2">b</a>'
        return "<html></html>"

    def empty_extract(_html: str, _url: str) -> dict[str, Any]:
        return {
            "title": None,
            "author": None,
            "date": None,
            "sitename": None,
            "hostname": "e.com",
            "description": None,
            "text": "",
        }

    monkeypatch.setattr(feed, "_fetch_html", fake_html)
    monkeypatch.setattr(feed, "extract_article", empty_extract)
    with pytest.raises(feed.WebScrapeError, match="none yielded an article"):
        feed.sync("https://e.com/list", link_pattern=r"aid=\d+", root=tmp_path)


# --------------------------------------------------------------------------- #
# Default mirror root
# --------------------------------------------------------------------------- #


def test_default_root_under_ava_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The skill script reads AVA_HOME from the env at call time (standalone,
    # no Settings import), so mutating the env mapping is effective here.
    monkeypatch.setitem(os.environ, "AVA_HOME", str(tmp_path))
    assert feed._default_root() == tmp_path / "state" / "mirrors" / "web-scrape"


def test_to_s1_uses_passed_root(tmp_path: Path) -> None:
    post: dict[str, Any] = {
        "url": "https://example.com/x",
        "title": "T",
        "text": "body",
        "author": {"id": "example.com", "name": "Example News"},
        "published_at": "2026-06-08",
        "fetched_at": "2026-06-08T00:00:00Z",
        "fetched_via": "web-scrape",
    }
    s1 = feed.to_s1(post, root=tmp_path)
    assert s1["raw_path"] == str(tmp_path / feed._url_slug(post["url"])) + "/"
    assert json.dumps(s1)  # serializable


# --------------------------------------------------------------------------- #
# fetch ladder: curl -> Jina escalation
# --------------------------------------------------------------------------- #

_WALL_HTML = "<html><head><title>Just a moment...</title></head><body>Enable JavaScript and cookies to continue</body></html>"

_JINA_RAW = """Title: Real Headline

URL Source: https://walled.example/article

Markdown Content:
# Real Headline

The full rendered body that only a server-side browser could reach, well past
the Cloudflare challenge that blocked the bare curl client.
"""


def test_split_jina_strips_header() -> None:
    title, body = feed._split_jina(_JINA_RAW)
    assert title == "Real Headline"
    assert body.startswith("# Real Headline")
    assert "Title:" not in body and "Markdown Content:" not in body


def test_has_wall_marker() -> None:
    assert feed._has_wall_marker(_WALL_HTML)
    assert not feed._has_wall_marker("<html><body><p>a normal article body</p></body></html>")


def test_fetch_escalates_to_jina_on_wall(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # curl returns a Cloudflare challenge shell (wall marker) -> ladder escalates
    # to the Jina render rung, whose body lands with a +jina provenance tag.
    monkeypatch.setattr(feed, "_fetch_html", lambda _url: _WALL_HTML)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        feed,
        "_jina_article",
        lambda _url: {  # pyright: ignore[reportUnknownArgumentType]
            "title": "Real Headline",
            "author": None,
            "date": None,
            "sitename": None,
            "hostname": "walled.example",
            "description": None,
            "text": "# Real Headline\n\nThe full rendered body.",
        },
    )
    post = feed.fetch("https://walled.example/article", root=tmp_path)
    assert post["fetched_via"] == "web-scrape+jina"
    assert "full rendered body" in post["text"]


def test_fetch_curl_403_escalates_to_jina(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # curl fails outright (403) -> still escalate to Jina rather than give up.
    def boom(_url: str) -> str:
        raise feed.WebScrapeError("curl failed (rc=22): 403")

    monkeypatch.setattr(feed, "_fetch_html", boom)
    monkeypatch.setattr(
        feed,
        "_jina_article",
        lambda _url: {  # pyright: ignore[reportUnknownArgumentType]
            "title": "T",
            "author": None,
            "date": None,
            "sitename": None,
            "hostname": "walled.example",
            "description": None,
            "text": "rendered body",
        },
    )
    post = feed.fetch("https://walled.example/article", root=tmp_path)
    assert post["fetched_via"] == "web-scrape+jina"


def test_fetch_index_page_does_not_escalate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An empty extraction with NO wall signature is an index/login page, not a
    # render wall: raise "use enum" and never burn a Jina call.
    monkeypatch.setattr(feed, "_fetch_html", lambda _url: "<html><body></body></html>")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        feed,
        "extract_article",
        lambda _html, _url: {  # pyright: ignore[reportUnknownArgumentType]
            "text": "",
            "title": None,
            "author": None,
            "date": None,
            "sitename": None,
            "hostname": "example.com",
            "description": None,
        },
    )

    def never(_url: str) -> dict[str, Any]:
        raise AssertionError("must not escalate a plain index page to Jina")

    monkeypatch.setattr(feed, "_jina_article", never)
    with pytest.raises(feed.WebScrapeError, match="no article body"):
        feed.fetch("https://example.com/list", root=tmp_path)


def test_fetch_both_rungs_walled_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # wall on curl AND a login-gate on Jina -> propagate a pointer to a logged-in
    # browser, not a silent empty.
    monkeypatch.setattr(feed, "_fetch_html", lambda _url: _WALL_HTML)  # pyright: ignore[reportUnknownArgumentType]

    def gated(_url: str) -> dict[str, Any]:
        raise feed.WebScrapeError("no body from ... via curl or jina render — login-gated")

    monkeypatch.setattr(feed, "_jina_article", gated)
    with pytest.raises(feed.WebScrapeError, match="login-gated"):
        feed.fetch("https://walled.example/article", root=tmp_path)


# --------------------------------------------------------------------------- #
# enum ladder: curl -> Jina escalation
# --------------------------------------------------------------------------- #

_MD_LIST = """Title: Forum Index

URL Source: https://walled.example/list

Markdown Content:
- [Article 101](https://walled.example/portal.php?mod=view&aid=101)
- [Article 102](https://walled.example/portal.php?mod=view&aid=102)
- [About](https://walled.example/about.html)
"""


def test_harvest_escalates_to_jina_on_curl_403(monkeypatch: pytest.MonkeyPatch) -> None:
    # curl is blocked outright (403) -> enum escalates to the Jina render rung,
    # whose markdown links are harvested in place of href="...".
    def boom(_url: str) -> str:
        raise feed.WebScrapeError("curl failed (rc=22): 403")

    monkeypatch.setattr(feed, "_fetch_html", boom)
    monkeypatch.setattr(feed, "_jina_render", lambda _url: _MD_LIST)  # pyright: ignore[reportUnknownArgumentType]
    links = feed.harvest_links("https://walled.example/list", r"mod=view&aid=\d+")
    assert links == [
        "https://walled.example/portal.php?mod=view&aid=101",
        "https://walled.example/portal.php?mod=view&aid=102",
    ]


def test_harvest_escalates_on_wall_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    # curl returns a Cloudflare challenge shell (200 but walled) -> escalate too.
    monkeypatch.setattr(feed, "_fetch_html", lambda _url: _WALL_HTML)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(feed, "_jina_render", lambda _url: _MD_LIST)  # pyright: ignore[reportUnknownArgumentType]
    links = feed.harvest_links("https://walled.example/list", r"mod=view&aid=\d+")
    assert len(links) == 2


def test_harvest_markdown_relative_links_absolutized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_url: str) -> str:
        raise feed.WebScrapeError("curl failed (rc=22): 403")

    md = "Title: X\n\nMarkdown Content:\n- [A](/portal.php?mod=view&aid=7)\n"
    monkeypatch.setattr(feed, "_fetch_html", boom)
    monkeypatch.setattr(feed, "_jina_render", lambda _url: md)  # pyright: ignore[reportUnknownArgumentType]
    links = feed.harvest_links("https://walled.example/list", r"aid=\d+")
    assert links == ["https://walled.example/portal.php?mod=view&aid=7"]


def test_harvest_no_escalation_when_curl_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a plain list page curl already fetched is NOT escalated (no Jina burn on a
    # pattern problem) — this is the existing rung-1 behavior, locked in.
    def never(_url: str) -> str:
        raise AssertionError("must not escalate a page curl already fetched")

    monkeypatch.setattr(feed, "_fetch_html", lambda _url: _LIST_HTML)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(feed, "_jina_render", never)
    links = feed.harvest_links("https://forum.example/portal.php", r"mod=view&aid=\d+")
    assert len(links) == 3


def test_harvest_both_rungs_fail_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_url: str) -> str:
        raise feed.WebScrapeError("curl failed (rc=22): 403")

    def refused(_url: str) -> str:
        raise feed.WebScrapeError("jina render of ...: the target refused the render (403)")

    monkeypatch.setattr(feed, "_fetch_html", boom)
    monkeypatch.setattr(feed, "_jina_render", refused)
    with pytest.raises(feed.WebScrapeError, match="target refused"):
        feed.harvest_links("https://walled.example/list", r"aid=\d+")


def test_sync_escalates_list_page_to_jina(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # the list page 403s curl but the Jina rung recovers it -> sync still works,
    # fetching each harvested article through the normal article path.
    def fake_html(url: str) -> str:
        if "portal.php" in url:
            return _ARTICLE_HTML
        raise feed.WebScrapeError("curl failed (rc=22): 403")

    monkeypatch.setattr(feed, "_fetch_html", fake_html)
    monkeypatch.setattr(feed, "_jina_render", lambda _url: _MD_LIST)  # pyright: ignore[reportUnknownArgumentType]
    items = feed.sync("https://walled.example/list", link_pattern=r"aid=\d+", root=tmp_path)
    assert [i["url"] for i in items] == [
        "https://walled.example/portal.php?mod=view&aid=101",
        "https://walled.example/portal.php?mod=view&aid=102",
    ]


def test_jina_render_target_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Jina echoes a target-side 403 (a host that refuses even a rendered client)
    # — must raise, not harvest a garbage page.
    class FakeProc:
        returncode = 0
        stdout = (
            b"Title: 403 Forbidden\n\nURL Source: https://blocked.example/list\n\n"
            b"Warning: Target URL returned error 403: Forbidden\n\nMarkdown Content:\n* * *\nwall"
        )
        stderr = b""

    def fake_run(*_args: object, **_kwargs: object) -> FakeProc:
        return FakeProc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(feed.WebScrapeError, match="target refused"):
        feed._jina_render("https://blocked.example/list")


# --------------------------------------------------------------------------- #
# platform tag (sibling adapters)
# --------------------------------------------------------------------------- #


def test_fetch_platform_tag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # a locally-installed adapter tags its items with its own platform value,
    # and the S1 projection carries the tag through.
    monkeypatch.setattr(feed, "_fetch_html", lambda _url: _ARTICLE_HTML)  # pyright: ignore[reportUnknownArgumentType]
    post = feed.fetch("https://example.com/news/headline", root=tmp_path, platform="local-news")
    assert post["platform"] == "local-news"
    assert feed.to_s1(post, root=tmp_path)["platform"] == "local-news"

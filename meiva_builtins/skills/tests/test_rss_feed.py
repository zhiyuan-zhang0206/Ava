from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

_PATH = (
    Path(__file__).parents[3]
    / "meiva_builtins"
    / "skills"
    / "integrations"
    / "web-sources"
    / "rss"
    / "scripts"
    / "feed.py"
)
_spec = importlib.util.spec_from_file_location("rss_feed_under_test", _PATH)
assert _spec and _spec.loader
feed = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = feed
_spec.loader.exec_module(feed)


def _rss(items_xml: str, title: str = "Example Feed") -> bytes:
    return (
        '<?xml version="1.0"?><rss version="2.0"'
        ' xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>'
        f"<title>{title}</title><link>https://ex.com</link>"
        f"{items_xml}</channel></rss>"
    ).encode()


_ITEM_FULL = """<item><title>First Post</title><link>https://ex.com/1</link>
<guid>https://ex.com/1</guid><pubDate>Sun, 08 Jun 2026 10:00:00 GMT</pubDate>
<description>Summary one with &lt;b&gt;markup&lt;/b&gt;</description>
<content:encoded>&lt;p&gt;Full body one&lt;/p&gt;</content:encoded></item>"""

_ITEM_OLD = """<item><title>Old Post</title><link>https://ex.com/old</link>
<guid>https://ex.com/old</guid><pubDate>Tue, 01 Jan 2019 00:00:00 GMT</pubDate>
<description>ancient</description></item>"""

_ITEM_UNDATED = """<item><title>No Date Post</title><link>https://ex.com/nd</link>
<guid>https://ex.com/nd</guid><description>no date here</description></item>"""


@pytest.fixture
def _mock_feed(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _install(xml_bytes: bytes) -> None:
        def fake_bytes(_url: str) -> bytes:
            return xml_bytes

        monkeypatch.setattr(feed, "_fetch_bytes", fake_bytes)

    return _install


def test_parse_feed_entries(_mock_feed: Any) -> None:
    _mock_feed(_rss(_ITEM_FULL))
    title, entries = feed.parse_feed("https://ex.com/feed")
    assert title == "Example Feed"
    assert len(entries) == 1
    meta = feed._entry_meta(entries[0])
    assert meta["id"] == "https://ex.com/1"
    assert meta["title"] == "First Post"
    assert meta["url"] == "https://ex.com/1"
    assert meta["published_at"] == "2026-06-08T10:00:00Z"


@pytest.mark.parametrize(
    "xml_bytes,should_raise",
    [
        pytest.param(
            b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title></channel></rss>',
            False,
            id="empty-rss20",
        ),
        pytest.param(
            b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>T</title></feed>',
            False,
            id="empty-atom10",
        ),
        pytest.param(
            b'<?xml version="1.0"?>'
            b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"'
            b' xmlns="http://purl.org/rss/1.0/">'
            b"<channel><title>T</title></channel></rdf:RDF>",
            False,
            id="empty-rss10-rdf",
        ),
        pytest.param(
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<rss version="2.0"><channel><title>T\xe9st</title></channel></rss>',
            False,
            id="charset-mismatch-benign-bozo",
        ),
        pytest.param(b"<html>not a feed</html>", True, id="html"),
        pytest.param(b'{"error": "not found"}', True, id="json"),
        pytest.param(
            b'<?xml version="1.0"?>'
            b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            b"</sitemapindex>",
            True,
            id="sitemap-xml",
        ),
        pytest.param(b"hello world", True, id="plain-text"),
        pytest.param(b"", True, id="empty-bytes"),
        pytest.param(
            b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</ti',
            True,
            id="truncated-fatal-bozo",
        ),
    ],
)
def test_parse_feed_empty_or_invalid(_mock_feed: Any, xml_bytes: bytes, should_raise: bool) -> None:
    _mock_feed(xml_bytes)
    if should_raise:
        with pytest.raises(feed.RssError, match="not recognized as a feed"):
            feed.parse_feed("https://ex.com/test")
    else:
        _title, entries = feed.parse_feed("https://ex.com/test")
        assert entries == []


def test_content_encoded_preferred_over_summary(_mock_feed: Any) -> None:
    _mock_feed(_rss(_ITEM_FULL))
    _title, entries = feed.parse_feed("https://ex.com/feed")
    assert feed._entry_content_html(entries[0]) == "<p>Full body one</p>"


def test_html_to_text_strips() -> None:
    assert feed._html_to_text("Summary one with <b>markup</b>") == "Summary one with markup"


def test_enum_since_drops_old_keeps_undated(_mock_feed: Any) -> None:
    _mock_feed(_rss(_ITEM_FULL + _ITEM_OLD + _ITEM_UNDATED))
    cutoff = time.mktime(time.strptime("2026-01-01", "%Y-%m-%d"))
    metas = feed.enum("https://ex.com/feed", since_ts=cutoff)
    titles = [m["title"] for m in metas]
    assert "Old Post" not in titles
    assert "First Post" in titles
    assert "No Date Post" in titles
    assert "_published_struct" not in metas[0]


def test_enum_limit(_mock_feed: Any) -> None:
    _mock_feed(_rss(_ITEM_FULL + _ITEM_OLD))
    metas = feed.enum("https://ex.com/feed", limit=1)
    assert len(metas) == 1
    assert metas[0]["title"] == "First Post"


def test_build_post_summary(_mock_feed: Any) -> None:
    _mock_feed(_rss(_ITEM_FULL))
    _title, entries = feed.parse_feed("https://ex.com/feed")
    post = feed.build_post("https://ex.com/feed", "Example Feed", entries[0])
    assert post["platform"] == "rss"
    assert post["author"]["id"] == "https://ex.com/feed"
    assert post["author"]["name"] == "Example Feed"
    assert post["text"] == "Full body one"
    assert post["source_id"] == "https://ex.com/1"


def test_build_post_full_uses_webscrape(_mock_feed: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_feed(_rss(_ITEM_FULL))
    _title, entries = feed.parse_feed("https://ex.com/feed")

    class FakeWS:
        WebScrapeError = RuntimeError

        @staticmethod
        def fetch_article(_url: str) -> tuple[dict[str, Any], str]:
            return {"text": "THE FULL ARTICLE BODY from the generic adapter"}, "web-scrape+jina"

    monkeypatch.setattr(feed, "_webscrape", lambda: FakeWS)
    post = feed.build_post("https://ex.com/feed", "Example Feed", entries[0], full=True)
    assert post["text"] == "THE FULL ARTICLE BODY from the generic adapter"
    assert post["_full"] is True


def test_build_post_full_falls_back_on_fetch_error(
    _mock_feed: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_feed(_rss(_ITEM_FULL))
    _title, entries = feed.parse_feed("https://ex.com/feed")

    class FailWS:
        WebScrapeError = RuntimeError

        @staticmethod
        def fetch_article(_url: str) -> tuple[dict[str, Any], str]:
            raise FailWS.WebScrapeError("no body via curl or jina — login-gated")

    monkeypatch.setattr(feed, "_webscrape", lambda: FailWS)
    post = feed.build_post("https://ex.com/feed", "Example Feed", entries[0], full=True)
    assert post["text"] == "Full body one"
    assert post["_full"] is False


def test_sync_writes_and_dedups(_mock_feed: Any, tmp_path: Path) -> None:
    _mock_feed(_rss(_ITEM_FULL))
    items = feed.sync("https://ex.com/feed", root=tmp_path)
    assert len(items) == 1
    s1 = items[0]
    assert s1["platform"] == "rss"
    assert s1["source_id"] == "https://ex.com/1"
    assert s1["author"]["id"] == "https://ex.com/feed"
    outdir = tmp_path / feed._entry_slug("https://ex.com/1")
    assert (outdir / "post.json").exists()
    assert s1["raw_path"] == str(outdir) + "/"


def test_sync_limit_counts_new_items_and_advances(_mock_feed: Any, tmp_path: Path) -> None:
    _mock_feed(_rss(_ITEM_FULL + _ITEM_OLD))
    r1 = feed.sync("https://ex.com/feed", limit=1, root=tmp_path)
    assert [i["source_id"] for i in r1] == ["https://ex.com/1"]
    r2 = feed.sync("https://ex.com/feed", limit=1, root=tmp_path)
    ids = {i["source_id"] for i in r2}
    assert "https://ex.com/old" in ids


def test_sync_full_then_summary_reuse(_mock_feed: Any, tmp_path: Path) -> None:
    _mock_feed(_rss(_ITEM_FULL))
    feed.sync("https://ex.com/feed", root=tmp_path)
    assert feed._load_if_complete("https://ex.com/1", full=True, root=tmp_path) is None
    assert feed._load_if_complete("https://ex.com/1", full=False, root=tmp_path) is not None


def test_default_root_under_meiva_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(os.environ, "MEIVA_HOME", str(tmp_path))
    assert feed._default_root() == tmp_path / "state" / "mirrors" / "rss"

#!/usr/bin/env python3
"""Thin CLI entry point for the generic web-scrape adapter.

The adapter logic lives in
`ava_builtins.skill_support.web_sources.webscrape` (its `fetch_article` /
`WebScrapeError` are also imported by the `rss` skill's `reference/feed.py`
for `--full` article-body extraction); see that module for the full docs.

Usage:
    .venv/bin/python skills/web-sources/generic/reference/feed.py fetch --url https://site/article
    .venv/bin/python skills/web-sources/generic/reference/feed.py enum --url https://site/list --link-pattern 'mod=view&aid=\\d+'
    .venv/bin/python skills/web-sources/generic/reference/feed.py sync --url https://site/list --link-pattern '...' --limit 20
"""

from __future__ import annotations

from ava_builtins.skill_support.web_sources.webscrape import main

if __name__ == "__main__":
    main()

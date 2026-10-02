#!/usr/bin/env python3
"""Thin CLI entry point for the generic web-scrape adapter.

The adapter logic lives in the parent skill's `webscrape` module (its
`fetch_article` / `WebScrapeError` are also imported by the `rss` skill's
`scripts/feed.py` for `--full` article-body extraction); see that module for
the full docs.

Usage:
    .venv/bin/python skills/web-sources/generic/scripts/feed.py fetch --url https://site/article
    .venv/bin/python skills/web-sources/generic/scripts/feed.py enum --url https://site/list --link-pattern 'mod=view&aid=\\d+'
    .venv/bin/python skills/web-sources/generic/scripts/feed.py sync --url https://site/list --link-pattern '...' --limit 20
"""

from __future__ import annotations

import sys
from pathlib import Path

# `webscrape` lives in the parent skill's scripts/ dir, a sibling tree within
# this same web-sources skill (not an importable package; PYTHONSAFEPATH=1
# keeps a script's own directory off sys.path). Structure Rule 6 recognizes
# this exact __file__-derived, within-skill shape as the endorsed guard.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

from webscrape import main

if __name__ == "__main__":
    main()

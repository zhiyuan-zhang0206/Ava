"""Shared logic for the web-sources skill family (generic / rss / youtube).

`webscrape.py` is the `generic` skill's web-scrape adapter (fetch/enum/sync +
the curl -> Jina fetch ladder); the `rss` skill's `reference/feed.py` imports
its `fetch_article` / `WebScrapeError` for `--full` article-body extraction.
"""

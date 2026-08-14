---
type: doc
title: ava.web — Web Access
description: '`ava.web` provides web search and concurrent content fetching. Search results come from a search engine, content fetching uses LLM reading comprehension (not raw HTML), batch fetch executes concurrently.'
tags:
- agent-view
- sdk
- agent-lifecycle
---

# ava.web — Web Access

## What it is

`ava.web` provides web search and concurrent content fetching. Search results come from a search engine, content fetching uses LLM reading comprehension (not raw HTML).

## Core API

- `search(queries, count=10, max_concurrent=None) → list[list[SearchResult]]` — Concurrently search multiple queries. `queries` is a list of strings; `count` max results per query (limit 20). Results returned in input order.
- `fetch(targets, max_chars=50000, max_concurrent=None) → list[str]` — Concurrently fetch multiple web pages and answer a `prompt` for each. `targets` is a list of `(url, prompt)` tuples; results returned in input order.

Both are batch-only. By default every item runs concurrently with no ceiling; pass `max_concurrent=N` to cap how many items are in flight at once (useful under a provider rate limit). Rate limiting beyond that belongs to the provider: a 429 propagates as `SearchError` / `FetchError` rather than being retried or backed off.

## Data Types
- `SearchResult`: title, url, snippet, kind (`kind` is the source partition: web / news / videos / discussions)

## Key Dependencies
- **Does not depend on browser service**: `search` uses Brave Search API (`ava/web.py:BRAVE_ENDPOINT`), `fetch` uses Jina Reader (`r.jina.ai`, server-side headless fetching); hitting a hard login wall raises `FetchError` (`_GATED_SITE_SKILLS` currently empty, login site adapters moved to private install)

## Notes
`fetch()` returns LLM-refined answers. For events/versions/releases after training cutoff, verify with `search` + `fetch`—do not rely on time-sensitive information in training data.

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

- `search(queries, count=10, max_concurrent=12) → list[list[SearchResult]]` — Concurrently search multiple queries. `queries` is a list of strings; `count` max results per query (limit 20). Results returned in input order.
- `fetch(targets, max_chars=50000, max_concurrent=12) → list[str]` — Concurrently fetch multiple web pages and answer a `prompt` for each. `targets` is a list of `(url, prompt)` tuples; results returned in input order.

Both are batch-only. By default each batch has a 12-item ceiling; pass a positive `max_concurrent=N` to choose another in-flight limit. The shared DeepSeek cap queues excess provider calls, and terminal failures propagate as `SearchError` / `FetchError`.

## Data Types
- `SearchResult`: title, url, snippet, kind (`kind` is the source partition: web / news / videos / discussions)

## Key Dependencies
- **Does not depend on browser service**: `search` uses Brave Search API (`AVA_WEB_BRAVE_SEARCH_ENDPOINT`, default `https://api.search.brave.com/res/v1/web/search`), `fetch` uses Jina Reader (`AVA_WEB_JINA_BASE_URL`, default `https://r.jina.ai/`, server-side headless fetching); hitting a hard login wall raises `FetchError` (`_GATED_SITE_SKILLS` currently empty, login site adapters moved to private install)

## Notes
`fetch()` returns LLM-refined answers. For events/versions/releases after training cutoff, verify with `search` + `fetch`—do not rely on time-sensitive information in training data.

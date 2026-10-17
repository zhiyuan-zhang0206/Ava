---
type: doc
title: meiva.web providers
description: 'Which engine answers a search and which reader returns a page, what each provider changes for the caller, and the request-rate contract both DeepSeek tools impose.'
tags:
- agent-view
- sdk
---

# meiva.web providers

Parent: [[web.meiva.okf.md]].

## Choosing

Each call's provider is configuration, not code:

| Setting | Default | Values | Selects |
|---|---|---|---|
| `MEIVA_WEB_SEARCH_PROVIDER` | `brave` | `brave`, `deepseek` | the engine behind `search()` |
| `MEIVA_WEB_FETCH_READER` | `jina` | `jina`, `deepseek` | the reader whose body `fetch()` answers from |
| `MEIVA_WEB_DEEPSEEK_SEARCH_ENDPOINT` | `https://api.deepseek.com/v1/tools/search` | URL | DeepSeek Search endpoint |
| `MEIVA_WEB_DEEPSEEK_FETCH_ENDPOINT` | `https://api.deepseek.com/v1/tools/fetch` | URL | DeepSeek Fetch endpoint |
| `MEIVA_WEB_DEEPSEEK_MAX_RESULTS` | 50 | int | DeepSeek's own result ceiling; `count` is clamped to it |
| `MEIVA_WEB_DEEPSEEK_QPS` | 5 | float | request starts per second `meiva.web` allows itself |

The defaults stay on the incumbent pair: adding a provider must not move them, and a switch is a decision, not a routine config edit. Both DeepSeek tools authenticate with `DEEPSEEK_API_KEY` — the key the `deepseek-*` models already use — so a model-capable installation needs no second credential.

## What a switch changes for the caller

**Search.** `kind` collapses to `web`: the DeepSeek tool returns one result list, so Brave's news / videos / discussions partitions are absent (a caller sees fewer `kind` values, never an error). `snippet` is the tool's snippet, or its 600-800 character `summary` when the snippet is empty. `rerank_score`, `authority`, `date` and `site_name` have no field on `SearchResult` and are dropped. The two engines order results differently rather than one dominating: DeepSeek's reranker favours primary and official sources and repeats one site's pages; Brave surfaces more popular tutorial content, keeps the query language, and honours `freshness` while DeepSeek honours query operators such as `after:`.

**Fetch.** DeepSeek's body is escaped Markdown (`\#`, `\.`, `https\:\/\/…`); `fetch()` still truncates at `max_chars`, still answers the prompt with the web-fetch model, and still runs the injection check on the body. There is no wall page to detect — an unreadable page is 403/404, surfaced as `FetchError` carrying the provider's message — so `_WALL_MARKERS` stays a Jina-path guard. Jina renders JavaScript and returns the full page including navigation; DeepSeek returns a cleaner, shorter body but does not render, and fails more often (one of five evaluation URLs).

## Request rate

DeepSeek allows 5 requests per second per account across Search and Fetch and answers 429 above it, so `meiva.web` paces DeepSeek request starts `1/MEIVA_WEB_DEEPSEEK_QPS` apart; a 12-wide batch queues locally instead of retrying. Measured 2026-10-17 with 10 queries at `max_concurrent=12`: unthrottled, 4 of 14 requests were 429 and the batch took 2.1s; paced to 5 QPS, none was rejected and it took 1.4s. The retry policy (3 attempts, exponential backoff, `Retry-After`) stays the second line of defence. Brave has no such pacing need at this workload: the same 10-query batch ran in 0.9s without a single 429.

The pacer is built where the deps are built (`wiring.py`) and handed to the provider through `RequestPacer`, so one SDK call cannot burst past the account limit. Two concurrent `meiva.web` calls in one process each pace themselves: the account limit is shared, the pacer is not.

```meiva-contract
id = "web-deepseek-provider"
kind = "invariant"
statement = "The DeepSeek path authenticates with DEEPSEEK_API_KEY, clamps count to the DeepSeek result ceiling, maps each result to SearchResult with kind web and the snippet-or-summary text, acquires the injected pacer before every request attempt so the account QPS holds, and raises SearchError or FetchError carrying the provider's own error message."
code = ["meiva/web/deepseek.py:<module>", "meiva/web/deepseek.py:_post._send", "meiva/web/deepseek.py:search_one", "meiva/web/deepseek.py:read_page"]
tests = ["meiva/tests/namespaces/web/test_web_deepseek.py::test_search_maps_deepseek_results", "meiva/tests/namespaces/web/test_web_deepseek.py::test_search_clamps_count_to_the_provider_cap", "meiva/tests/namespaces/web/test_web_deepseek.py::test_batch_request_starts_respect_the_configured_qps", "meiva/tests/namespaces/web/test_web_deepseek.py::test_fetch_reports_an_unreadable_page"]
```

```meiva-contract
id = "web-provider-selection"
kind = "invariant"
statement = "web_search_provider and web_fetch_reader select the provider when the deps are built, both default to the incumbent brave and jina, and a provider switch leaves the fetch answer step and the injection check unchanged."
code = ["meiva/web/wiring.py:search_deps_for", "meiva/web/wiring.py:fetch_deps_for", "meiva/web/runner.py:run_search", "meiva/web/reader.py:read_page"]
tests = ["meiva/tests/namespaces/web/test_web_deepseek.py::test_default_provider_is_unchanged", "meiva/tests/namespaces/web/test_web_deepseek.py::test_fetch_reads_the_page_from_the_deepseek_endpoint", "meiva/tests/namespaces/web/test_web_deepseek.py::test_search_deps_reads_provider_settings"]
```

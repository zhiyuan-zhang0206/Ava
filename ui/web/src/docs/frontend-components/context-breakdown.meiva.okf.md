---
type: doc
title: Context breakdown presentation
description: Current and historical context cards, category display and inline error semantics.
tags: [frontend]
---

# Context breakdown presentation

The composer mounts the breakdown lazily and refetches on every open. Its live meter retains its own values; the expanded body gets totals and thresholds from the response, anchors in place and expands upward (not a modal). The run timeline supplies a message point and snapshot instead: it shows the request at or after that index, with session and time in the heading. A null point or a 404 means no LLM request; other failures stay visible inline with Retry. HTTP status remains authoritative even when the gateway supplies a JSON detail instead of an HTTP-prefixed message. While the point changes, the preceding result stays visible until its replacement arrives.

The real input total anchors category percentages. The display merges context_note and automation into System notes because both are system-injected content; it keeps the API kinds distinct. It sums their tokens, keeps the estimated flag if either input has it, then sorts display rows by token share; rows are color-coded by category and the output row is labeled "Text output". Equal shares retain the constructed display order; the merged System notes row was appended after the other categories. Unknown categories retain their raw names so new backend kinds stay visible. Category rows become toggle buttons only when the caller maps them to timeline blocks. The entire recursive sections block starts collapsed, as do nested sections. Empty categories with no input total receive explicit no-data copy.

The historical card uses the supplied message point and snapshot; the current-context body fetches independently of live meter props. A 404 historical point is empty, while other HTTP failures and network failures have distinct inline explanations and retry. Totals and thresholds come from the fetched response.

Parent: [[ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md]].

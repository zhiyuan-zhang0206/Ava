---
type: doc
title: Context breakdown presentation
description: Current and historical context cards, category display and inline error semantics.
tags: [frontend]
---

# Context breakdown presentation

The composer mounts the breakdown lazily and refetches on every open. Its live meter retains its own values; the expanded body gets totals and thresholds from the response, anchors in place and expands upward (not a modal). The run timeline supplies a message point and snapshot instead: it shows the request at or after that index, with session and time in the heading. A null point or a 404 means no LLM request; other failures stay visible inline with Retry. HTTP status remains authoritative even when the gateway supplies a JSON detail instead of an HTTP-prefixed message. While the point changes, the preceding result stays visible until its replacement arrives.

The real input total anchors category percentages. The display merges context_note and automation into System notes because both are system-injected content; it keeps the API kinds distinct. It sums their tokens, keeps the estimated flag if either input has it, then sorts display rows by token share; rows are color-coded by category and the output row is labeled "Text output". Equal shares retain the constructed display order; the merged System notes row was appended after the other categories. Unknown categories retain their raw names so new backend kinds stay visible. Category rows become toggle buttons only when the caller maps them to timeline blocks. The entire recursive sections block starts collapsed, as do nested sections. Empty categories with no input total receive explicit no-data copy.


```ava-contract
id = "context-point-and-failure"
kind = "invariant"
statement = "The historical card uses the supplied message point and snapshot; the current-context body fetches independently of live meter props. A 404 historical point is empty, while other HTTP failures and network failures have distinct inline explanations and retry. Totals and thresholds come from the fetched response."
code = ["ui/web/src/components/inspector/context-breakdown-body.tsx:ContextBreakdownCard", "ui/web/src/components/inspector/context-breakdown-body.tsx:ContextBreakdownBody", "ui/web/src/components/inspector/context-breakdown-body.tsx:BreakdownQuery"]
tests = ["ui/web/src/components/inspector/context-breakdown.test.tsx::ContextBreakdownCard (P4-3)::thresholds in the panel come from the fetched response, not the live meter props", "ui/web/src/components/inspector/context-breakdown.test.tsx::ContextBreakdownCard (P4-3)::a point with no LLM request (404) is the neutral empty state, not a red failure", "ui/web/src/components/inspector/context-breakdown.test.tsx::ContextBreakdownCard (P4-3)::an error answer carrying a JSON detail is classified as a server error, not unreachable", "ui/web/src/components/inspector/context-breakdown.test.tsx::ContextButton::classifies a network failure as gateway-unreachable and Retry refetches"]
```

```ava-contract
id = "context-category-display"
kind = "invariant"
statement = "Category shares use the input total. System-note markers and automation are merged only for display, with summed tokens and propagated estimation, then sorted by descending tokens. Unknown category names remain visible. Sections begin collapsed; categories without corresponding timeline blocks are labels rather than toggle buttons."
code = ["ui/web/src/components/inspector/context-breakdown-body.tsx:BreakdownContent"]
tests = ["ui/web/src/components/inspector/context-breakdown.test.tsx::ContextButton::merges context_note + automation into one 'System notes' row (summed tokens)", "ui/web/src/components/inspector/context-breakdown.test.tsx::ContextButton::legend rows sort by context share, largest first (not the backend's fixed order)", "ui/web/src/components/inspector/context-breakdown.test.tsx::ContextButton::the whole section block is collapsed by default and disclosed by its own chevron (same as a section row)", "ui/web/src/app/insights/run/[agents]/page.test.tsx::legend highlight::links to the context breakdown rows: a row lights its class, and the legend lights its row"]
```

Parent: [[ui/web/src/docs/frontend-components/frontend-components.ava.okf.md]].

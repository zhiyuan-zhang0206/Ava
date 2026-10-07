---
title: Agent messages bounded default
---

# Agent messages bounded default

`GET /api/agents/{id}/messages` now returns at most the newest 100 raw
checkpoint messages when `limit` is omitted. The same default applies when a
caller supplies only the exclusive `before` cursor, so neither implicit request
form can load an unbounded checkpoint payload.

An explicit `limit` keeps the established inclusive `1..10000` range. A caller
that needs the complete history pages backward by passing the returned
`start_index` as its next `before` value. `msg_count` remains the total history
length.

The response adds `has_more`: it is true precisely when messages exist before
the returned window. No `next_before` field was added because `start_index` is
already that exact exclusive cursor; duplicating it would create two names for
the same wire value.

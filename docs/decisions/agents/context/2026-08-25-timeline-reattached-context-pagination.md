# Timeline pagination excludes reattached context

## Context

Timeline window responses include standing context ahead of their historical
tail: the system prompt and the latest inbound compact summary. Treating the
first parseable response item as the next `before` cursor therefore paged from
the compact summary instead of the oldest real tail item. That request could
return only context with `has_more=false`, permanently disabling further
scroll-up loads in the frontend store.

The same distinction controls prepend anchoring. Reattached context remains at
the array and DOM head when older history lands, so it cannot signal a landing
or measure the displacement of the content the user is reading.

## Decision

The frontend owns one predicate for the two gateway-reattached kinds and uses
it for both page-cursor selection and all prepend-anchor semantics. Pagination
starts from the oldest stable non-context item. Anchor capture and landing
detection likewise use the first visible or array-front non-context item,
falling back to a context node only when no real item is visible and zero
compensation is correct.

Changing gateway window composition was rejected for this fix because standing
context is an intentional response contract. Adding an explicit page-cursor
field remains a possible protocol improvement, but was deferred to keep the
repair local to the consumer that confused response context with page data.

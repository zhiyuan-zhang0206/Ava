# Fleet graph token window and cache bounds

Fleet graph `total_tokens` is the input-plus-output counter increase over the
retained seven-day window. Raw Prometheus counter values were rejected because
exporter restarts reset them, making an all-time contract neither stable nor
reproducible.

The message-edge decay constant accepts values from zero through ten and is
rounded to two decimal places before both graph computation and cache-key
construction. This keeps computed graphs aligned with their cache entries and
bounds the parameter-driven Redis key space. A per-caller limiter was deferred
because the endpoint is auth-gated and the key space is now finite.
The seven-day window stays well inside Prometheus's native 90d retention
(deploy/lgtm/native, storage.tsdb.retention.time), so the range selector is
always valid on the production store.

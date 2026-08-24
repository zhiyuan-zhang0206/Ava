# Stats dashboard Loki fan-out

## Root cause

The cold 168-hour dashboard load issued 116 Loki queries under the shared
four-query budget: four full-window `llm_usage` aggregate scans and 112
six-hour event-stat shards. During the unlabeled legacy slice, every shard
still scanned the full stream, so narrower slices did not substantially reduce
cost: a measured six-hour turn query took 4.65 seconds and the equivalent
24-hour query took 5.0 seconds. A cluster-wide 24-hour `llm_usage` count took
8.38 seconds; the 168-hour token scans were the most expensive queries.

## Fix

The dashboard now sums complete UTC days from `agent_model_tokens_daily` and
queries only Loki's leading edge and retained ledger gap tail for tokens and
cost. Turn-duration sum/count and warning/error counts remain live Loki reads,
but use 12-hour shards; warning/error levels share one grouped query per shard.
The result is 50 cold 168-hour queries: at most eight token-tail aggregates,
28 turn aggregates, and 14 grouped warning/error queries.

## After legacy expiry

The unlabeled legacy slice expires at 2026-08-30 11:10Z. The ledger-first
split remains in place after that date: it continues to avoid full-window
token scans while the indexed Loki tail rereads the newest retained ledger day
to absorb late writes without double counting.

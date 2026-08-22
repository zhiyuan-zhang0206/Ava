# Agent status model contraction

The lifecycle model now removes the bookkeeping-only allocated and starting
states. New, resurrected, respawned, and swapped-in rows are unclaimed
idling; the child claims ownership through one idling-to-running CAS that
writes pid, start time, and lease together.

Restarting remains the durable cross-process replacement intent. Hibernating
remains ops-only and continues to project as idling outside operations.

The migration first normalizes legacy rows to idling, then narrows the status
check constraint. Its down migration widens only the constraint because prior
row meanings cannot be reconstructed.

The boot/dead-birth safeguards now use row ownership columns: the aged reaper
selects unclaimed idling rows, launch confirmation waits for a non-null pid, and
a dead claimed row with no produced message enters the crash-resurrect backoff
path instead of an immediate revive.

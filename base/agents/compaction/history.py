"""Unsettled meaningful input excludes a quiescent source; IDs are not commit watermarks."""

ADMISSION_SQL = (
    "SELECT 1 FROM inbound_messages WHERE agent_id=%s "
    "AND kind IN ('chat','compact_request','compact_summary') "
    "AND status IN ('pending','claimed') LIMIT 1"
)

# The saver selects these rows as CheckpointTuple.pending_writes for this exact
# root head. Ancestor delta writes are already consumed by reconstruction and
# are deliberately outside this query. Unknown task channels are not guessed
# to be harmless administrative projections.
PENDING_HISTORY_SQL = (
    "SELECT 1 FROM checkpoint_writes w WHERE w.thread_id=%s AND w.checkpoint_ns='' "
    "AND w.checkpoint_id=(SELECT checkpoint_id FROM checkpoints "
    "WHERE thread_id=%s AND checkpoint_ns='' ORDER BY checkpoint_id DESC LIMIT 1) LIMIT 1"
)

# Find the run — checkpoints and conversation content

The database answers "what was actually said". Loki answers "what happened".
Come here when you need the messages themselves, or an agent's own history.

## Getting a psql prompt

There is no `ava psql` verb. `AVA_DB_URL` normally points at the cluster's
PgBouncer listener, which is transaction-pooled — fine for a one-shot query,
hostile to interactive exploration (`SET`, prepared statements, advisory locks
all misbehave). Derive the direct URL the way the admin plane does:

```bash
psql "$(.venv/bin/python -c 'from shared.db import direct_db_url; print(direct_db_url())')"
```

Pass that URL verbatim — it already carries whatever the cluster's auth needs
(a no-secret cluster's loopback Postgres is `trust`; a secret'd one authenticates
as its own role), plus a `hostaddr=` that a hand-rebuilt string would lose.

## The checkpoint tables

LangGraph's `PostgresSaver` creates them at runtime, so they are **not** in
`db/schema.sql` — a fresh or test database may not have them at all.

- `checkpoints (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, checkpoint JSONB, metadata JSONB)`
- `checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type, blob BYTEA)`
- `checkpoint_writes (...)`

Facts that shape every query:

- **`thread_id` is `str(agents.id)`** — the decimal agent id as TEXT.
- **`checkpoint_ns` is always `''`** in Ava.
- **`checkpoint_id` is a UUIDv6**, so it sorts lexicographically by time:
  `ORDER BY checkpoint_id DESC` is newest-first, everywhere.
- **`messages` is a channel, and it is msgpack in `checkpoint_blobs.blob`.**
  SQL can enumerate channels and locate blobs; it cannot read the message list.
  Content needs Python (below).
- The `messages` channel is `Annotated[list, add_messages]` — a plain
  accumulator — so **every checkpoint holds the entire message list**, not a
  delta. Any single surviving checkpoint is a complete conversation snapshot.

## Map an agent's surviving checkpoints

```sql
SELECT checkpoint_id,
       parent_checkpoint_id,
       metadata ->> 'trace_id' AS trace_id,
       COALESCE((metadata ->> 'compact_boundary')::boolean, false) AS is_boundary
FROM checkpoints
WHERE thread_id = '3048' AND checkpoint_ns = ''
ORDER BY checkpoint_id DESC;
```

Reverse direction — which agent and checkpoint a trace id belongs to:

```sql
SELECT thread_id::bigint AS agent_id, checkpoint_id
FROM checkpoints
WHERE metadata ->> 'trace_id' = '<lowercase-32-hex>';
```

## Retention: what still exists

The checkpoint reaper (`services/events_maintenance/checkpoint_reaper.py`)
trims hard — a terminated or 24h-inactive thread keeps 1 checkpoint, a live
thread over 20 keeps 5. Assume recent state is readable and older state is
not, **except** for compaction boundaries.

## Reading history across compaction segments

Every compaction stamps the thread's newest checkpoint
`metadata->>'compact_boundary' = true` (`shared/agents/history/checkpoint_cleanup.py:mark_compact_boundary`),
and the reaper's predicate excludes those rows from every trim:

```sql
NOT COALESCE((metadata ->> 'compact_boundary')::boolean, false)
```

So each past compaction segment survives as exactly one full snapshot. To walk
an agent's whole past:

```sql
SELECT checkpoint_id
FROM checkpoints
WHERE thread_id = '3048'
  AND COALESCE((metadata ->> 'compact_boundary')::boolean, false)
ORDER BY checkpoint_id ASC;   -- oldest segment first
```

Then load each boundary's message list in turn. No retention machinery is
involved — this is a query over what is already kept.

## Reading the messages

```python
from shared.agents.history.checkpoint import load_checkpoint_messages, load_checkpoint_messages_by_trace

msgs = load_checkpoint_messages(3048)                       # the agent's current state
ckpt_id, msgs = load_checkpoint_messages_by_trace(3048, trace_id)   # by trace
```

`load_checkpoint_messages_by_trace` resolves `metadata->>'trace_id'` to a
`checkpoint_id`, then reads the blob through `PostgresSaver` with the msgpack
allowlist. `(None, [])` means the checkpoint was trimmed — the content is gone
even though its span metadata survives.

For an arbitrary checkpoint (a compaction boundary, say), go through the saver
directly with the id in the config:

```python
saver.get_tuple({"configurable": {"thread_id": "3048", "checkpoint_id": "<uuid6>"}})
```

Always construct the serde with the allowlist
(`JsonPlusSerializer(allowed_msgpack_modules=STATIC_CHECKPOINT_MSGPACK_TYPES)`,
`shared/agents/history/checkpoint_serde.py`) or every load spews deserialization warnings.

## Over HTTP instead

The gateway exposes the same resolution, so an off-box agent needs no database
access:

```
GET /api/agents/{agent_id}/traces/{trace_id}/messages
GET /api/agents/{agent_id}/messages
```

Bearer `AVA_CLUSTER_SECRET` when the cluster has one; gateway is on port 8000.
`pruned: true` is the trimmed-checkpoint shape, not an error; 404 means the
agent is gone. `scripts/read_trace.py --with-content` calls this for you.

## The Postgres `events` archive is gone

The `events` table was frozen at the LGTM cutover (2026-08-12, task #1197) and
dropped with the archive cleanup (task #1281/#1823). Historical reads go to the
Loki archive stream ([event-stream](event-stream.md)); the cold pg_dump archive
(task #1823) holds the pre-drop copy.

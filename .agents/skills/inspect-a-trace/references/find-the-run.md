# Find the run — checkpoints and conversation content

The database answers "what was actually said". Loki answers "what happened".
Come here when you need the messages themselves, or an agent's own history.

## Getting a psql prompt

There is no `meiva psql` verb. `MEIVA_DB_URL` normally points at the cluster's
PgBouncer listener, which is transaction-pooled — fine for a one-shot query,
hostile to interactive exploration (`SET`, prepared statements, advisory locks
all misbehave). Derive the direct URL the way the admin plane does:

```bash
psql "$(.venv/bin/python - <<'PY'
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.db.config import db_config_from_boot

boot = ConfigBoot()
boot.boot()
machine = machine_from_boot(boot)
resolve = ClusterDirectUrl(machine.host, gateway_url_reader=lambda: boot.view.gateway.gateway_url)
print(resolve(db_config_from_boot(boot)))
PY
)"
```

Pass that URL verbatim — it already carries whatever the cluster's auth needs
(Postgres always authenticates, as the role of the delivered write generation), plus a `hostaddr=` that a hand-rebuilt string would lose.

## The checkpoint tables

LangGraph's `PostgresSaver` creates them at runtime, so they are **not** in
`db/schema.sql` — a fresh or test database may not have them at all.

- `checkpoints (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, checkpoint JSONB, metadata JSONB)`
- `checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type, blob BYTEA)`
- `checkpoint_writes (...)`

Facts that shape every query:

- **`thread_id` is `str(agents.id)`** — the decimal agent id as TEXT.
- **`checkpoint_ns` is always `''`** in Meiva.
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

Checkpoints are never trimmed automatically (never-delete ruling): every
checkpoint of a thread is still readable.

## Reading history across compaction segments

Every compaction stamps the thread's newest checkpoint
`metadata->>'compact_boundary' = true` (`base/agents/history/compact_boundary.py:mark_compact_boundary`),
so each past compaction segment ends at exactly one stamped full snapshot. To walk
an agent's whole past:

```sql
SELECT checkpoint_id
FROM checkpoints
WHERE thread_id = '3048'
  AND COALESCE((metadata ->> 'compact_boundary')::boolean, false)
ORDER BY checkpoint_id ASC;   -- oldest segment first
```

Then load each boundary's message list in turn.

## Reading the messages

```python
from base.agents.history.checkpoint import load_checkpoint_messages, load_checkpoint_messages_by_trace

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
`base/agents/history/checkpoint_serde.py`) or every load spews deserialization warnings.

## Over HTTP instead

The gateway exposes the same resolution, so an off-box agent needs no database
access:

```
GET /api/agents/{agent_id}/traces/{trace_id}/messages
GET /api/agents/{agent_id}/messages
```

Bearer `MEIVA_API_TOKEN` (or, on the gateway, `MEIVA_CLUSTER_SECRET`) when the
cluster authenticates; gateway is on port 8000.
`pruned: true` is the trimmed-checkpoint shape, not an error; 404 means the
agent is gone. `scripts/read_trace.py --with-content` calls this for you.

## The Postgres `events` archive is gone

The `events` table was frozen at the LGTM cutover (2026-08-12, task #1197) and
dropped with the archive cleanup (task #1281/#1823). Historical reads go to the
Loki archive stream ([event-stream](event-stream.md)); the cold pg_dump archive
(task #1823) holds the pre-drop copy.

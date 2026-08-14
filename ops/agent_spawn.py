"""Agent birth: a NEW agents_meta row, optionally forked from another agent.

One of the two lifecycle halves reached through `ops/agents.py`; the other is
`ops/agent_wake.py`, which revives rows that already exist. This side owns the
*GATEWAY-side creation* of a birth — the row insert, the fork checkpoint copy,
and the pre-launch inbound delivery. Task #1236 follow-up: the row must be
created as the MAIN data-plane identity, so creation happens on the gateway,
never on the target runner — its ops server dials as the least-privilege
`ava_runner` role, which by design cannot INSERT agents / agents_meta. The
*mechanics* of launching a detached native child process live in
`ops/agent_launch.py` (module-qualified access), reached from the runner's
`launch` op.

- **create_agent_row(*, spawner="user", fork_from=None, fork_checkpoint=None,
  machine=<target>)** — new agent + new agents_meta row, NO launch; returns
  `(new_id, birth_config)`. `spawner` string ("user" / "agent:N" / arbitrary
  external trigger name); frontend uses it to build the tree. `fork_from` +
  `fork_checkpoint` must be passed as a pair (the caller — the gateway routing
  layer — resolves "latest" to an explicit id first). The launch half is the
  runner's `launch` op: detached child launch + launch-confirm scheduling
  (agents_meta UPDATE — within the runner role) + the plain-spawn first prompt
  (inbound INSERT — within the runner role too).
"""

from __future__ import annotations

import json
import re

import psycopg

import shared.db
from shared.agents import ForkCheckpointNotFound
from shared.audit_events import insert_event_log
from shared.birth_config import resolve_birth_config
from shared.db import fetch_one, insert_inbound_message
from shared.live_announce import publish_agent_spawned_sync
from shared.log import logger


def latest_checkpoint_id(cur: psycopg.Cursor, agent_id: int) -> str | None:
    """Latest checkpoint_id for the source agent; None if empty.

    LangGraph's checkpoint_id is UUIDv7 / ULID-style (time-prefix); lex order
    is equivalent to time order — `ORDER BY checkpoint_id DESC LIMIT 1` gets
    latest.

    Heartbeat skip: when the most recent inbound(s) are heartbeats (idle agent
    nudges), the latest checkpoint reflects heartbeat processing, not a real
    conversation turn. Forking from that state would seed the new agent with
    heartbeat noise. We count consecutive heartbeat inbounds at the top of the
    stack and skip that many checkpoints so the fork starts from the last
    meaningful turn — the timeline's last message, not a heartbeat response.

    Used by gateway `POST /api/agents` fork path — resolves "latest" and
    passes an explicit ckpt id into `create_agent_row(fork_checkpoint=...)` for the
    actual copy. The SDK no longer calls this function (SDK uses HTTP and lets
    the gateway resolve).
    """
    # LangGraph PostgresSaver schema: checkpoints.thread_id is framework
    # column naming (kept as-is); we cast Ava agent_id to str() here.
    # Count consecutive heartbeat inbounds at the top of the stack — each
    # heartbeat that the agent processed created one checkpoint, so skipping
    # N checkpoints lands us on the last pre-heartbeat conversation turn.
    cur.execute(
        "SELECT kind FROM inbound_messages WHERE agent_id = %s ORDER BY id DESC",
        (agent_id,),
    )
    skip_count = 0
    for (kind,) in cur:
        if kind == "heartbeat":
            skip_count += 1
        else:
            break

    cur.execute(
        "SELECT checkpoint_id FROM checkpoints WHERE thread_id = %s "
        "ORDER BY checkpoint_id DESC LIMIT 1 OFFSET %s",
        (str(agent_id), skip_count),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _copy_checkpoint_chain(
    cur: psycopg.Cursor,
    source_agent_id: int,
    source_checkpoint_id: str,
    new_agent_id: int,
) -> None:
    """Copy the source agent's LangGraph state at source_checkpoint_id as the
    new_agent's first checkpoints.

    Rows copied:
    - **checkpoints**: target ckpt_id + all its ancestors (recursively follow
      parent_checkpoint_id), so the new agent sees the full history chain
    - **checkpoint_blobs**: all rows from the source agent — blobs are
      uniqued by (thread, ns, channel, version); the new agent reads
      corresponding channel/version on demand. Extra blobs that go unread do
      not affect behavior
    - **Does NOT** copy checkpoint_writes: those are mid-step pending writes;
      fork sees the clean post-commit snapshot

    LangGraph checkpoints / blobs tables have `thread_id` as TEXT (framework
    schema unchanged) — agents_meta.id is BIGINT, this function explicitly
    str()-casts when writing into thread_id.

    Raises:
        ForkCheckpointNotFound: source_checkpoint_id does not exist on the
            source agent (recursive CTE INSERT 0 rows).
    """
    # LangGraph schema preserved: checkpoints.thread_id / checkpoint_blobs.thread_id
    # are framework column names (hardcoded in PostgresSaver); we cast Ava agent_id to str() here.
    cur.execute(
        """
        WITH RECURSIVE chain AS (
            SELECT * FROM checkpoints
             WHERE thread_id = %(src)s AND checkpoint_id = %(ckpt)s
            UNION ALL
            SELECT c.*
              FROM checkpoints c
              JOIN chain ON c.thread_id = %(src)s
                       AND c.checkpoint_id = chain.parent_checkpoint_id
        )
        INSERT INTO checkpoints (
            thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
            type, checkpoint, metadata
        )
        SELECT %(new)s, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
               type, checkpoint, metadata
          FROM chain
        """,
        {"src": str(source_agent_id), "ckpt": source_checkpoint_id, "new": str(new_agent_id)},
    )
    if cur.rowcount == 0:
        raise ForkCheckpointNotFound(
            f"checkpoint {source_checkpoint_id!r} does not exist in thread {source_agent_id}"
        )
    cur.execute(
        """
        INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, version, type, blob)
        SELECT %(new)s, checkpoint_ns, channel, version, type, blob
          FROM checkpoint_blobs WHERE thread_id = %(src)s
        """,
        {"src": str(source_agent_id), "new": str(new_agent_id)},
    )


_SPAWNER_AGENT_RE = re.compile(r"^agent:(\d+)$")


def _spawner_agent_id_malformed(spawner: str) -> bool:
    """Return True when a spawner that starts with 'agent:' has a
    non-numeric or non-positive id part — a sign of a caller bug where
    the spawning process's own agent id was None/unset.

    Accepts: "agent:1", "agent:405" etc.
    Rejects: "agent:None", "agent:", "agent:abc", "agent:0".
    """
    m = _SPAWNER_AGENT_RE.match(spawner)
    if m is None:
        return True
    return int(m.group(1)) <= 0


def create_agent_row(
    *,
    spawner: str = "user",
    fork_from: int | None = None,
    fork_checkpoint: str | None = None,
    machine: str,
    config: dict[str, object] | None = None,
    label: str | None = None,
    prompt: str | None = None,
    prompt_source: str | None = None,
) -> tuple[int, dict[str, object] | None]:
    """Create the agent row: agents + agents_meta + fork copy, NO launch.

    The DB half of what used to be `spawn_agent` (Task #1236 follow-up): the
    row must be created by the GATEWAY as the main data-plane identity — the
    target runner's ops server runs as the least-privilege `ava_runner` role,
    which by design cannot INSERT agents / agents_meta. The gateway resolves
    the fork checkpoint, creates the row (status='allocated'), and forwards a
    launch-only op to the target runner; `_launch_agent_process` + the
    launch-confirm then run on the runner (agents_meta UPDATE is within the
    runner role). `machine` is the TARGET host (the row's placement) — always
    explicit here, never "local".

    Returns `(new_id, birth_config)` — birth_config rides the launch op so the
    child replays the same stamp the row was born with.

    A fork (fork_from set) auto-INSERTs a kind='fork' lifecycle inbound and
    copies the source's checkpoint chain in the same transaction; a fork
    `prompt` is delivered pre-launch as a chat inbound. A plain spawn delivers
    no inbound here — its first prompt lands post-launch from the launch op.

    Args:
        spawner: identifier of the entity that triggered the spawn — "user" /
            "agent:<id>" / arbitrary ("claude-code" etc.). Frontend builds a
            tree by the spawner field (agent:N relations) + gives non-agent:
            prefixes their own root section. Default "user" lets admin /
            scripts / browser buttons call directly without passing.
        fork_from: source agent_id. If given, copy its state at
            fork_checkpoint into the new agent. Must be passed together with
            fork_checkpoint (None or both).
        fork_checkpoint: exact checkpoint id of the source agent. LangGraph
            checkpoints are append-only; "latest" drifts under concurrent
            writes — the caller (gateway routing layer) resolves latest and
            passes an explicit id here.
        machine: target physical host (placement) — written to
            `agents_meta.machine`; subsequent resurrect / restart land on it.
        config: optional per-agent overlay (currently `{"llm_model": ...}`),
            persisted to agents_meta.config_overlay and applied at child boot.
            None = cluster defaults. Every `lifecycle="frozen"` field NOT named
            here is resolved from the current cluster default and stamped into
            agents_meta.birth_config in the same INSERT, so a later default flip
            leaves this agent where it was born (shared/birth_config.py).
        label: optional initial label (the spawner assigning the new agent's
            role). When given, it is written with label_user_set=TRUE so the
            labeler's CAS treats it as already-set and does not overwrite it.
            None = leave NULL (labeler may auto-generate one if a prompt is given).
        prompt: optional chat message delivered pre-launch (forks only);
            paired with prompt_source (both None or both given).
        prompt_source: provenance tag for `prompt` ('agent:N' / 'user').

    Returns:
        (new agent_id, birth_config dict).

    Raises:
        ForkCheckpointNotFound: fork_checkpoint does not exist on fork_from.
        ValueError: prompt / prompt_source not provided as a pair.
    """
    if (fork_from is None) != (fork_checkpoint is None):
        raise ValueError(
            "fork_from and fork_checkpoint must be provided as a pair (both None or both given)"
        )
    if (prompt is None) != (prompt_source is None):
        raise ValueError(
            "prompt and prompt_source must be provided as a pair (both None or both given)"
        )
    if spawner.startswith("agent:") and _spawner_agent_id_malformed(spawner):
        raise ValueError(
            f"spawner has agent: prefix but the id part is not a valid agent id: "
            f"{spawner!r}. This is often caused by an un-bootstrapped process "
            f"(ava._boot.establish never called) — the process's own agent id "
            f"was None, producing 'agent:None'. Fix the caller to establish "
            f"identity before spawning."
        )
    # The gateway creates the row for ANY target (the runner's ops server runs
    # as ava_runner and cannot INSERT agents); the launch op re-checks the
    # agent-runner capability on the target itself.
    target_machine = machine

    with shared.db.connect() as conn, conn.cursor() as cur:
        # label: when the spawner assigns one, store it sticky (label_user_set=TRUE)
        # so the labeler's CAS (WHERE label IS NULL AND NOT label_user_set) skips it.
        # Otherwise leave NULL — the labeler generates a short name via LLM CAS when
        # spawn carries a prompt; without prompt / on LLM failure the label stays
        # NULL and the frontend displays the fallback "#N".
        if label:
            cur.execute(
                "INSERT INTO agents (label, label_user_set) VALUES (%s, TRUE) RETURNING id",
                (label,),
            )
        else:
            cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        new_id: int = fetch_one(cur, "spawn: insert agent")[0]
        # THE spawn boundary is this INSERT — every spawn in the system funnels
        # through it (SDK / frontend / scripts all POST /api/agents, which
        # dispatches the launch to the target runner), so it is the one place
        # the frozen-field stamp is taken. A fork carries its parent's stamp over
        # verbatim: a fork is the same identity continuing, so it must not silently
        # re-resolve its brain against today's cluster defaults.
        inherited: dict[str, object] | None = None
        if fork_from is not None:
            cur.execute("SELECT birth_config FROM agents_meta WHERE id = %s", (fork_from,))
            inherited = fetch_one(cur, "spawn: read fork source birth_config")[0]
        birth_config = resolve_birth_config(cur, config, inherited=inherited)
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, fork_source_agent_id, "
            "fork_source_checkpoint_id, status, machine, config_overlay, birth_config) "
            "VALUES (%s, %s, %s, %s, 'allocated', %s, %s::jsonb, %s::jsonb)",
            (
                new_id,
                spawner,
                fork_from,
                fork_checkpoint,
                target_machine,
                json.dumps(config) if config else None,
                json.dumps(birth_config, sort_keys=True),
            ),
        )
        if fork_from is not None and fork_checkpoint is not None:
            # ForkCheckpointNotFound causes the whole transaction to roll back (the with block does not commit)
            _copy_checkpoint_chain(cur, fork_from, fork_checkpoint, new_id)
            # The copied history reads as the source agent's identity to the new
            # process. INSERT a kind='fork' lifecycle inbound in THIS transaction
            # (committed before launch) so the new agent's first claim appends an
            # identity marker before any LLM turn. source carries the lineage
            # "agent:{fork_from}" (intrinsic — independent of `spawner`); the
            # claim node renders the new id from its own config.
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                "VALUES (%s, '', 'fork', %s)",
                (new_id, f"agent:{fork_from}"),
            )
        conn.commit()
        # --- lifecycle event ---
        # Emitted AFTER the commit above: the emitter's drain thread writes on
        # its own connection (READ COMMITTED) and cannot see an uncommitted
        # agents row. Emitting before commit made the fresh agent_id look
        # dangling — the guard cleared the reference and the event_log mirror
        # row (agent_id NOT NULL) was silently skipped, losing the FleetView
        # parent/child edge for every spawn. After commit the row is visible
        # and the mirror row lands.
        # Parse spawner for the optional source agent (agent:N → target=N).
        spawner_target: int | None = None
        if spawner.startswith("agent:"):
            import contextlib

            with contextlib.suppress(ValueError):
                spawner_target = int(spawner.removeprefix("agent:"))
        event_type = "fork" if fork_from is not None else "spawn"
        insert_event_log(
            event_type=event_type,
            agent_id=new_id,
            source=spawner,
            target_agent_id=spawner_target,
            payload={
                "machine": target_machine,
                "fork_from": fork_from,
                "fork_checkpoint": fork_checkpoint,
            },
        )
        # Fork prompt: deliver BEFORE launch as a separate transaction (not
        # folded into the checkpoint-copy txn — insert_inbound_message commits
        # on its own). The forked agent inherits a full history, so its first
        # claim would dispatch the committed fork marker and start a turn on
        # the inherited task before a post-launch prompt lands — committing here
        # puts it in that first batch. Same pre-launch delivery as resurrect
        # (no arrival).
        if prompt is not None:
            assert prompt_source is not None, "prompt requires prompt_source (validated above)"  # noqa: S101
            if label:
                prompt = f"{prompt}\n\nYour label has been set to {label}."
            insert_inbound_message(conn, new_id, prompt, source=prompt_source)
        # Publish AgentSpawned right after DB commit — the frontend sidebar adds
        # the new row immediately (status='allocated'); the agent process's own
        # status transitions later publish AgentUpdated to advance it.
        publish_agent_spawned_sync(conn, new_id)
    # Launch is the runner's job now (the launch op) — the row is created and
    # the caller forwards it. The `agent_spawned` telemetry event keeps its
    # registered name (contract.py) — the row INSERT is still the spawn
    # boundary, the launch is its second half. Return the id + the birth stamp
    # the launch op must replay.
    logger.info(
        "agent {agent_id} row created by {spawner}",
        event="agent_spawned",
        agent_id=new_id,
        spawner=spawner,
        forked_from=fork_from,
        machine=target_machine,
    )
    return new_id, birth_config

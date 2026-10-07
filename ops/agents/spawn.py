"""Agent birth: a NEW agents_meta row, optionally forked from another agent.

One of the two lifecycle halves reached through the `ops.agents` door; the other
is `ops.agents.wake`, which revives rows that already exist. This side owns the
*GATEWAY-side creation* of a birth — the row insert, the fork checkpoint copy,
and the pre-launch inbound delivery. Task #1236 follow-up: the row must be
created as the MAIN data-plane identity, so creation happens on the gateway,
never on the target runner — its ops server dials as the least-privilege
`ava_runner` role, which by design cannot INSERT agents / agents_meta. The
runner's `launch` op (`ops.lifecycle.launch`) validates the created row and
wakes its host.

- **create_agent_row(*, spawner="user", fork_from=None, fork_checkpoint=None,
  machine=<target>)** — new agent + new agents_meta row, NO launch; returns
  `(new_id, birth_config, prompt_inbound_id, launch_attempt_id)`. `spawner` string ("user" / "agent:N" / arbitrary
  external trigger name); frontend uses it to build the tree. `fork_from` +
  `fork_checkpoint` must be passed as a pair (the caller — the gateway routing
  layer — resolves "latest" to an explicit id first). The launch half is the
  runner's `launch` op: validation and a repeatable hosted wake. Every first prompt is in the
  gateway's row-creation transaction, before the launch forward.
"""

from __future__ import annotations

from uuid import UUID

import psycopg

from base import telemetry
from base.db import Database, announce_spawn_prompt
from base.events.live.announce import publish_agent_spawned_sync
from base.events.live.bus import EventBus
from base.log import logger
from ops.agents.birth_transaction import _SPAWNER_AGENT_RE as _SPAWNER_AGENT_RE
from ops.agents.birth_transaction import _spawner_agent_id_malformed as _spawner_agent_id_malformed
from ops.agents.birth_transaction import insert_agent_birth
from ops.agents.birth_transaction import validate_spawn_args as _validate_spawn_args


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


def _announce_created_agent(
    db: Database,
    bus: EventBus,
    agent_id: int,
    birth_event: telemetry.Event,
    prompt_inbound_id: int | None,
    prompt_event: telemetry.Event | None,
) -> None:
    """Emit the recorded audit events and live hints after the birth transaction commits."""
    telemetry.emit_prepared(birth_event)
    if prompt_inbound_id is not None:
        try:
            announce_spawn_prompt(db, bus, agent_id, prompt_inbound_id, prompt_event)
        except Exception:
            logger.exception("agent {} prompt announcement failed", agent_id)
    try:
        publish_agent_spawned_sync(bus, agent_id)
    except Exception:
        logger.exception("agent {} roster announcement failed", agent_id)


def create_agent_row(
    db: Database,
    bus: EventBus,
    *,
    spawner: str = "user",
    fork_from: int | None = None,
    fork_checkpoint: str | None = None,
    machine: str,
    config: dict[str, object] | None = None,
    label: str | None = None,
    prompt: str | None = None,
    prompt_source: str | None = None,
    preset_name: str | None = None,
    fork_tail_skills: list[str] | None = None,
    creation_key: str | None = None,
    creation_request_hash: str | None = None,
) -> tuple[int, dict[str, object] | None, int | None, UUID]:
    """Create the agent row: agents + agents_meta + fork copy, NO launch.

    The DB half of what used to be `spawn_agent` (Task #1236 follow-up): the
    row must be created by the GATEWAY as the main data-plane identity — the
    target runner's ops server runs as the least-privilege `ava_runner` role,
    which by design cannot INSERT agents / agents_meta. The gateway resolves
    the fork checkpoint, creates the row (unclaimed status='idling'), and forwards a
    launch-only op to the target runner; the hosted runner validates and wakes
    the pending work. `machine` is the TARGET host (the row's placement) — always
    explicit here, never "local".

    Returns `(new_id, birth_config, prompt_inbound_id, launch_attempt_id)`.

    A fork (fork_from set) auto-INSERTs a kind='fork' lifecycle inbound and
    copies the source's checkpoint chain in the same transaction. Every first
    prompt is committed as a chat inbound with the row, before launch.

    Args:
        spawner: identifier of the entity that triggered the spawn — "user" /
            "agent:<id>" / arbitrary ("claude-code" etc.). Frontend builds a
            tree by the spawner field (agent:N relations) + gives non-agent:
            prefixes their own root section. Default "user" lets admin /
            scripts / browser buttons call directly without passing.
            For a fork the stored spawner is the fork SOURCE (the lineage
            parent, "agent:<fork_from>") — NOT the executor who triggered the
            fork; the executor stays traceable via the fork event's `source`
            and the fork prompt inbound's source (user ruling 2026-08-28,
            task #1879).
        fork_from: source agent_id. If given, copy its state at
            fork_checkpoint into the new agent. Must be passed together with
            fork_checkpoint (None or both).
        fork_checkpoint: exact checkpoint id of the source agent. LangGraph
            (see below).
        preset_name: the spawn-time preset reference, stored for display next
            to the RESOLVED config_overlay (docs/decisions/2026-09-10-preset-in-
            config-overlay-fork-cache.md); None = no preset.
        fork_tail_skills: skill names a fork's config added to
            skills_to_inject_into_system_prompt (minus its expand list); carried
            in the fork inbound payload so the claim node grafts their bodies at
            the context tail. Ignored unless fork_from is set.
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
            leaves this agent where it was born (base/agents/birth_config.py).
        label: optional initial label (the spawner assigning the new agent's
            role). When given, it is written with label_user_set=TRUE so the
            labeler's CAS treats it as already-set and does not overwrite it.
            None = leave NULL (labeler may auto-generate one if a prompt is given).
        prompt: optional chat message committed with the row;
            paired with prompt_source (both None or both given).
        prompt_source: provenance tag for `prompt` ('agent:N' / 'user').
        creation_key: optional scoped identity for one creation intent.
        creation_request_hash: immutable caller request digest paired with the key;
            same-key races return the previously committed agent identity.

    Returns:
        (new agent_id, birth_config dict, chat inbound id, launch attempt id).

    Raises:
        ForkCheckpointNotFound: fork_checkpoint does not exist on fork_from.
        ValueError: prompt / prompt_source not provided as a pair.
    """
    _validate_spawn_args(spawner, fork_from, fork_checkpoint, prompt, prompt_source)
    with db.connect() as conn, conn.cursor() as cur:
        conn.execute("SET TRANSACTION READ WRITE")
        birth = insert_agent_birth(
            cur,
            spawner=spawner,
            fork_from=fork_from,
            fork_checkpoint=fork_checkpoint,
            machine=machine,
            config=config,
            label=label,
            prompt=prompt,
            prompt_source=prompt_source,
            preset_name=preset_name,
            fork_tail_skills=fork_tail_skills,
            creation_key=creation_key,
            creation_request_hash=creation_request_hash,
        )
        if birth.birth_event is None:
            return birth.legacy_result()
        conn.commit()
        _announce_created_agent(
            db,
            bus,
            birth.agent_id,
            birth.birth_event,
            birth.prompt_inbound_id,
            birth.prompt_event,
        )
    logger.info(
        "agent {agent_id} row created by {spawner}",
        event="agent_spawned",
        agent_id=birth.agent_id,
        spawner=spawner,
        forked_from=fork_from,
        machine=machine,
    )
    return birth.legacy_result()

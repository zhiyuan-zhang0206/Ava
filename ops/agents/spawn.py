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

import json
import re
from uuid import UUID, uuid4

import psycopg

import base.db
from base import telemetry
from base.agents.birth_config import resolve_birth_config
from base.agents.history.checkpoint_copy import copy_checkpoint_chain
from base.agents.impersonation_manifest import record_central_event
from base.agents.labels import spawn_prompt_with_label
from base.db import announce_spawn_prompt, fetch_one, insert_spawn_prompt_in_transaction
from base.events.live.announce import publish_agent_spawned_sync
from base.events.live.bus import EventBus
from base.lm.registry import normalize_overlay_llm_model
from base.log import logger
from base.telemetry.audit_events import prepare_event_log, record_audit


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


def _record_birth_event(
    conn: psycopg.Connection,
    agent_id: int,
    spawner: str,
    fork_from: int | None,
    fork_checkpoint: str | None,
    target_machine: str,
) -> telemetry.Event:
    """Record the spawn or fork audit fact in the birth transaction.

    Who spawned whom cannot be derived from any later state, so the row commits
    with the agent row or not at all. A fork's audit parent is the source agent,
    even when a third agent executed it.
    """
    spawner_target: int | None = None
    if spawner.startswith("agent:"):
        spawner_target = int(spawner.removeprefix("agent:"))
    event = prepare_event_log(
        event_type="fork" if fork_from is not None else "spawn",
        agent_id=agent_id,
        source=spawner,
        target_agent_id=fork_from if fork_from is not None else spawner_target,
        payload={
            "machine": target_machine,
            "fork_from": fork_from,
            "fork_checkpoint": fork_checkpoint,
        },
    )
    return record_audit(conn, record_central_event(conn, event))


def _announce_created_agent(
    agent_id: int,
    birth_event: telemetry.Event,
    prompt_inbound_id: int | None,
    prompt_event: telemetry.Event | None,
) -> None:
    """Emit the recorded audit events and live hints after the birth transaction commits."""
    telemetry.emit_prepared(birth_event)
    if prompt_inbound_id is not None:
        try:
            announce_spawn_prompt(agent_id, prompt_inbound_id, prompt_event)
        except Exception:
            logger.exception("agent {} prompt announcement failed", agent_id)
    try:
        publish_agent_spawned_sync(EventBus.from_settings(), agent_id)
    except Exception:
        logger.exception("agent {} roster announcement failed", agent_id)


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
    preset_name: str | None = None,
    fork_tail_skills: list[str] | None = None,
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
            to the RESOLVED config_overlay (decisions/2026-09-10-preset-in-
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

    Returns:
        (new agent_id, birth_config dict, chat inbound id, launch attempt id).

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
            f"(ava.agent_identity.establish never called) — the process's own agent id "
            f"was None, producing 'agent:None'. Fix the caller to establish "
            f"identity before spawning."
        )
    # The gateway creates the row for ANY target (the runner's ops server runs
    # as ava_runner and cannot INSERT agents); the launch op re-checks the
    # agent-runner capability on the target itself.
    target_machine = machine

    launch_attempt_id = uuid4()
    prompt_inbound_id: int | None = None
    prompt_event: telemetry.Event | None = None
    with base.db.connect() as conn, conn.cursor() as cur:
        conn.execute("SET TRANSACTION READ WRITE")
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
        if config:
            # Last-mile settlement (task #4306): the gateway preflight already
            # normalized + reported a withdrawn llm_model; keeping the rewrite
            # at the row itself means a withdrawn id never lands in
            # agents_meta.config_overlay, whatever client path composed the map
            # (a bare fork copies the source overlay over verbatim).
            config = dict(config)
            model_receipt = normalize_overlay_llm_model(config)
            if model_receipt is not None:
                logger.warning(
                    "spawn overlay llm_model {requested!r} is withdrawn; stored "
                    "the registered fallback {resolved!r} (task #4306)",
                    event="spawn_overlay_model_normalized",
                    requested=model_receipt[0],
                    resolved=model_receipt[1],
                )
        birth_config = resolve_birth_config(cur, config, inherited=inherited)
        # For a fork, spawner records the fork SOURCE — the lineage parent
        # (user ruling 2026-08-28, task #1879). The executor who triggered the
        # fork stays traceable via the fork event's `source` and the fork
        # prompt inbound's source; it is not what the spawner column means.
        lineage_spawner = f"agent:{fork_from}" if fork_from is not None else spawner
        # The first life's epoch fence is 0: nothing predates it, so it
        # supersedes nothing (every reader treats it like NULL). No retired
        # runtime stamped a birth epoch, so it is also this runtime's proof of
        # the row's origin: an unadmitted row it later ends by force stays
        # resurrectable (`base.agents.incarnation.lifecycle_acceptance.record_unowned_termination`).
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, born_spawner, fork_source_agent_id, "
            "fork_source_checkpoint_id, status, machine, config_overlay, birth_config, preset_name, "
            "last_launch_attempt_id, last_resurrect_inbound_id) "
            "VALUES (%s, %s, %s, %s, %s, 'idling', %s, %s::jsonb, %s::jsonb, %s, %s, 0)",
            (
                new_id,
                lineage_spawner,
                lineage_spawner,
                fork_from,
                fork_checkpoint,
                target_machine,
                json.dumps(config) if config else None,
                json.dumps(birth_config, sort_keys=True),
                preset_name,
                launch_attempt_id,
            ),
        )
        if fork_from is not None and fork_checkpoint is not None:
            # ForkCheckpointNotFound causes the whole transaction to roll back (the with block does not commit)
            copy_checkpoint_chain(cur, fork_from, fork_checkpoint, new_id)
            # The copied history reads as the source agent's identity to the new
            # process. INSERT a kind='fork' lifecycle inbound in THIS transaction
            # (committed before launch) so the new agent's first claim appends an
            # identity marker before any LLM turn. source carries the lineage
            # "agent:{fork_from}" (intrinsic — independent of `spawner`); the
            # claim node renders the new id from its own config.
            # The fork inbound carries the tail-graft skill delta (skills the
            # fork's config added to skills_to_inject_into_system_prompt beyond
            # the source's, minus what the fork's expand list already grafts).
            # The claim node's fork handler appends their SKILL.md bodies at the
            # context tail — the inherited prefix stays byte-identical for the
            # provider cache. payload NULL = nothing to graft (legacy forks too).
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
                "VALUES (%s, '', 'fork', %s, %s::jsonb)",
                (
                    new_id,
                    f"agent:{fork_from}",
                    json.dumps({"tail_skills": fork_tail_skills}) if fork_tail_skills else None,
                ),
            )
        if prompt is not None:
            assert prompt_source is not None, "prompt requires prompt_source (validated above)"  # noqa: S101
            prompt_content = spawn_prompt_with_label(prompt, label)
            prompt_inbound_id, prompt_event = insert_spawn_prompt_in_transaction(
                cur, new_id, prompt_content, prompt_source
            )
        birth_event = _record_birth_event(
            conn, new_id, spawner, fork_from, fork_checkpoint, target_machine
        )
        conn.commit()
        # The recorded events reach the observation sink and the live hints go
        # out only after commit.
        _announce_created_agent(new_id, birth_event, prompt_inbound_id, prompt_event)
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
    return new_id, birth_config, prompt_inbound_id, launch_attempt_id

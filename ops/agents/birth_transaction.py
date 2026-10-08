"""Agent birth effects inside a caller-owned transaction; no commit, launch or announce."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg

from base import telemetry
from base.agents.birth_config import resolve_birth_config
from base.agents.history.checkpoint_copy import copy_checkpoint_chain
from base.agents.impersonation.manifest import record_central_event
from base.agents.labels import spawn_prompt_with_label
from base.db import fetch_one, insert_spawn_prompt_in_transaction
from base.lm.registry import normalize_overlay_llm_model
from base.log import logger
from base.telemetry.audit_events import prepare_event_log, record_audit
from ops.agents.creation_identity import record_creation_snapshot, recover_birth


@dataclass(frozen=True)
class AgentBirth:
    """Transaction result plus audit facts the caller may emit after commit."""

    agent_id: int
    birth_config: dict[str, object] | None
    prompt_inbound_id: int | None
    launch_attempt_id: UUID
    birth_event: telemetry.Event | None
    prompt_event: telemetry.Event | None

    def legacy_result(self) -> tuple[int, dict[str, object] | None, int | None, UUID]:
        """Preserve the public create_agent_row result, including replay's absent prompt."""
        return self.agent_id, self.birth_config, self.prompt_inbound_id, self.launch_attempt_id


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


def record_birth_event(
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


def validate_spawn_args(
    spawner: str,
    fork_from: int | None,
    fork_checkpoint: str | None,
    prompt: str | None,
    prompt_source: str | None,
) -> None:
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
            f"(no AvaContext carrying an agent id was bound) — the process's own agent id "
            f"was None, producing 'agent:None'. Fix the caller to bind "
            f"an identity before spawning."
        )


def _insert_agent_identity(cur: psycopg.Cursor[Any], label: str | None) -> int:
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
    return new_id


def _resolve_birth_overlay(
    cur: psycopg.Cursor[Any],
    config: dict[str, object] | None,
    fork_from: int | None,
) -> tuple[dict[str, object] | None, dict[str, object]]:
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
    return config, birth_config


def _insert_fork_history(
    cur: psycopg.Cursor[Any],
    new_id: int,
    fork_from: int | None,
    fork_checkpoint: str | None,
    fork_tail_skills: list[str] | None,
) -> None:
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


def insert_agent_birth(
    cur: psycopg.Cursor[Any],
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
    immutable_creation_snapshot: bool = False,
) -> AgentBirth:
    """Write/replay the existing birth, first prompt, fork and audit in this transaction.

    Caller owns commit/rollback and post-commit announcements. Replay returns
    no new birth event, prompt event or prompt inbound id.
    """
    validate_spawn_args(spawner, fork_from, fork_checkpoint, prompt, prompt_source)
    if immutable_creation_snapshot and (creation_key is None or creation_request_hash is None):
        raise ValueError("creation snapshot requires keyed identity")
    # The gateway creates the row for ANY target (the runner's ops server runs
    # as ava_runner and cannot INSERT agents); the launch op re-checks the
    # agent-runner capability on the target itself.
    target_machine = machine
    conn = cur.connection

    launch_attempt_id = uuid4()
    prompt_inbound_id: int | None = None
    prompt_event: telemetry.Event | None = None
    existing = recover_birth(
        conn, creation_key, creation_request_hash, immutable_snapshot=immutable_creation_snapshot
    )
    if existing is not None:
        return AgentBirth(
            existing.agent_id, existing.birth_config, None, existing.launch_attempt_id, None, None
        )
    new_id = _insert_agent_identity(cur, label)
    config, birth_config = _resolve_birth_overlay(cur, config, fork_from)
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
        "last_launch_attempt_id, last_resurrect_inbound_id, creation_key, creation_request_hash) "
        "VALUES (%s, %s, %s, %s, %s, 'idling', %s, %s::jsonb, %s::jsonb, %s, %s, 0, %s, %s)",
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
            creation_key,
            creation_request_hash,
        ),
    )
    _insert_fork_history(cur, new_id, fork_from, fork_checkpoint, fork_tail_skills)
    if prompt is not None:
        assert prompt_source is not None, "prompt requires prompt_source (validated above)"  # noqa: S101
        prompt_content = spawn_prompt_with_label(prompt, label)
        prompt_inbound_id, prompt_event = insert_spawn_prompt_in_transaction(
            cur, new_id, prompt_content, prompt_source
        )
    if immutable_creation_snapshot:
        if creation_key is None or creation_request_hash is None:
            raise ValueError("creation snapshot requires keyed identity")
        record_creation_snapshot(
            conn,
            key=creation_key,
            request_hash=creation_request_hash,
            agent_id=new_id,
            machine=target_machine,
            config=config,
            birth_config=birth_config,
            launch_attempt_id=launch_attempt_id,
            prompt_inbound_id=prompt_inbound_id,
            prompt_content=spawn_prompt_with_label(prompt, label) if prompt is not None else None,
            prompt_source=prompt_source,
        )
    birth_event = record_birth_event(
        conn, new_id, spawner, fork_from, fork_checkpoint, target_machine
    )
    return AgentBirth(
        new_id, birth_config, prompt_inbound_id, launch_attempt_id, birth_event, prompt_event
    )

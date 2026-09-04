"""Retry an exact never-admitted birth using the existing process controller.

The original marker owns its deadline and counter. Missing prior native attempt
evidence is uncertainty, not permission to allocate another process. No OS work
occurs while publication/metadata locks are held.
"""

from uuid import UUID

import psutil
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

import shared.db
from shared.cluster import session_name
from shared.db_transaction import write_transaction
from shared.incarnation_resources import ResourceBirth, ResourceEvidenceError, decode_resources
from shared.machine import machine_name
from shared.paths import run_dir
from shared.runtime_admission import process_runtime_admission, require_activation
from shared.session_record import SessionRecord


def birth_session(agent_id: int, birth: UUID, attempt: int) -> str:
    return session_name(f"boot-{agent_id}-resource-{birth.hex}-{attempt}")


def _previous_ended(agent_id: int, birth: ResourceBirth) -> bool:
    if birth.launch_attempts == 0:
        return True
    record = SessionRecord.read(
        run_dir()
        / "sessions"
        / f"{birth_session(agent_id, birth.birth, birth.launch_attempts)}.json"
    )
    if record is None or record.create_time <= 0:
        return False
    try:
        native = psutil.Process(record.pid)
        return native.create_time() != record.create_time or native.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True
    except psutil.AccessDenied:
        return False


def launch_birth(agent_id: int, *, confirm: bool = False) -> str | None:
    """None means legacy/successor, never a deferred managed birth."""
    with shared.db.connect() as conn:
        snapshot = conn.execute(
            "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone()
    if snapshot is None or snapshot[0] is None:
        return None
    state = decode_resources(snapshot[0])
    if not isinstance(state, ResourceBirth):
        return None
    if not _previous_ended(agent_id, state):
        raise ResourceEvidenceError("previous birth attempt is live or has unknown identity")
    publication = process_runtime_admission()
    local_machine = machine_name()
    with write_transaction() as conn:
        require_activation(conn, publication.decide(conn))
        row = conn.execute(
            "SELECT incarnation_resources,machine,status,pid,runtime_generation,runtime_owner,lifecycle_command_id,config_overlay,birth_config "
            "FROM agents_meta WHERE id=%s FOR UPDATE",
            (agent_id,),
        ).fetchone()
        clock = conn.execute("SELECT clock_timestamp()").fetchone()
        if (
            row is None
            or row[0] != snapshot[0]
            or row[1:7] != (local_machine, "idling", None, None, None, None)
            or clock is None
            or state.launch_deadline is None
            or clock[0] >= state.launch_deadline
            or state.launch_attempts >= state.launch_limit
        ):
            raise ResourceEvidenceError(
                "birth changed, is unarmed, or exhausted its original budget"
            )
        allocated = state.model_copy(update={"launch_attempts": state.launch_attempts + 1})
        conn.execute(
            "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
            (Jsonb(allocated.model_dump(mode="json")), agent_id),
        )
        remaining = (state.launch_deadline - clock[0]).total_seconds()
        config, birth_config = row[7:]
    from ops import agent_launch

    attempt = agent_launch._launch_agent_process(
        agent_id,
        config_overlay=config,
        birth_config=birth_config,
        confirm=False,
        resource_attempt=(state.birth, allocated.launch_attempts, remaining),
    )
    if confirm:
        agent_launch._wait_for_agent_claim(agent_id, attempt)
    return attempt


def resume_births(pool: ConnectionPool, machine: str, limit: int) -> list[int]:
    """An existing controller pass revisits preserved births after stable publication."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id FROM agents_meta WHERE machine=%s AND status='idling' AND pid IS NULL "
            "AND incarnation_resources->>'state'='unadmitted' ORDER BY id LIMIT %s",
            (machine, limit),
        ).fetchall()
    resumed: list[int] = []
    from shared.log import logger
    from shared.runtime_admission import PublicationAdmissionDeferredError

    for row in rows:
        try:
            if launch_birth(row[0]) is not None:
                resumed.append(row[0])
        except (ResourceEvidenceError, PublicationAdmissionDeferredError) as exc:
            logger.info("resource birth remains deferred", agent_id=row[0], reason=str(exc))
    return resumed

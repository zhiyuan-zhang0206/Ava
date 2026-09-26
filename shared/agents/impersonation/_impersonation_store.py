"""Short, agent-row-serialized transactions for cooperative impersonation."""

import hashlib
import hmac
import math
import re
import subprocess
import sys
from typing import Any, cast
from uuid import UUID

import psutil
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from shared.caller_identity import caller_payload
from shared.machine import machine_name
from shared.native_process import native_boot_id
from shared.native_process.ownership import OwnedProcess
from shared.runtime_incarnation import RuntimeIncarnation

OPEN = ("requested", "accepted", "active")


class ImpersonationError(RuntimeError):
    """The lease, local placement, or expected ownership does not permit work."""


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def lock_agent(conn: psycopg.Connection, agent_id: int) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,))
        row = cur.fetchone()
    if row is None:
        raise ImpersonationError(f"Agent {agent_id} does not exist")
    return row


def lock_lease(conn: psycopg.Connection, lease_id: str) -> dict[str, Any]:
    # Read only the immutable foreign key first; all mutations acquire the agent
    # lock before the lease or inbox, matching native ownership and claim order.
    row = conn.execute(
        "SELECT agent_id FROM agent_impersonations WHERE id=%s", (UUID(str(lease_id)),)
    ).fetchone()
    if row is None:
        raise ImpersonationError("Impersonation does not exist")
    lock_agent(conn, row[0])
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM agent_impersonations WHERE id=%s FOR UPDATE", (lease_id,))
        lease = cur.fetchone()
    if lease is None:
        raise ImpersonationError("Impersonation disappeared")
    return lease


def public(lease: dict[str, Any]) -> dict[str, Any]:
    return {
        key: str(value) if isinstance(value, UUID) else value
        for key, value in lease.items()
        if key not in ("token_hash", "relay_token_hash")
    }


def local(lease: dict[str, Any]) -> None:
    if lease["machine"] != machine_name():
        raise ImpersonationError("Impersonation is limited to the agent's own machine")


_PROVIDER_ANCHOR_BASENAMES = frozenset({"codex", "claude"})

# claude's native installer runs a version-stamped binary from
# ``<install>/claude/versions/<version>`` (the process carries the version as
# its name), so its controller node never matches the basename allowlist.
_CLAUDE_NATIVE_VERSION = re.compile(r"\d+(?:\.\d+)+")


def _basename(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    return value.rstrip("/").rsplit("/", 1)[-1].lower()


def _metadata_nodes(metadata: object) -> list[dict[str, Any]]:
    """One recorded/observed chain as head plus ancestors, skipping junk."""
    if not isinstance(metadata, dict):
        return []
    meta = cast("dict[str, Any]", metadata)
    nodes: list[dict[str, Any]] = []
    if meta.get("pid") is not None:
        nodes.append(
            {
                key: meta[key]
                for key in ("pid", "name", "executable", "created_at", "starttime", "boot_id")
                if key in meta
            }
        )
    ancestors = meta.get("ancestors")
    if isinstance(ancestors, list):
        nodes.extend(
            cast("dict[str, Any]", node)
            for node in cast("list[object]", ancestors)
            if isinstance(node, dict)
        )
    return nodes


def _is_claude_native_executable(executable: object) -> bool:
    """Whether a recorded executable is a claude native-install artifact.

    Recognition is the install shape — a ``claude`` directory holding a
    ``versions`` directory whose entry is version-stamped (e.g.
    ``~/.local/share/claude/versions/2.1.274``). Exactly like the basename
    allowlist, this is a shape gate on what gets recorded; the attestation
    itself requires the recorded native process birth and boot identity.
    """
    if not isinstance(executable, str) or not executable:
        return False
    parts = executable.rstrip("/").split("/")
    if len(parts) < 3 or parts[-2].lower() != "versions" or parts[-3].lower() != "claude":
        return False
    return _CLAUDE_NATIVE_VERSION.fullmatch(parts[-1]) is not None


def _is_provider_node(node: dict[str, Any]) -> bool:
    executable = node.get("executable")
    return (
        _basename(node.get("name")) in _PROVIDER_ANCHOR_BASENAMES
        or _basename(executable) in _PROVIDER_ANCHOR_BASENAMES
        or _is_claude_native_executable(executable)
    )


def _process_identity(node: dict[str, Any]) -> tuple[OwnedProcess, str | None] | None:
    """Read complete native evidence; incomplete historical nodes stay unknown."""
    if not {"pid", "created_at", "starttime", "boot_id"} <= node.keys():
        return None
    pid, birth, ticks, boot = (node[key] for key in ("pid", "created_at", "starttime", "boot_id"))
    if type(pid) is not int or pid <= 0:
        return None
    if not _valid_birth(birth):
        return None
    if not _valid_ticks(ticks):
        return None
    if not _valid_boot_id(boot):
        return None
    return OwnedProcess(pid, birth, ticks), boot


def _valid_birth(birth: object) -> bool:
    if isinstance(birth, bool) or not isinstance(birth, (int, float)):
        return False
    try:
        return math.isfinite(birth) and birth > 0
    except OverflowError:
        return False


def _valid_ticks(ticks: object) -> bool:
    if sys.platform == "linux":
        return type(ticks) is int and ticks > 0
    return ticks is None


def _valid_boot_id(boot: object) -> bool:
    if sys.platform == "win32":
        return boot is None
    if not isinstance(boot, str):
        return False
    try:
        return str(UUID(boot)) == boot
    except ValueError:
        return False


def _same_process(anchor: dict[str, Any], node: dict[str, Any]) -> bool:
    recorded, observed = _process_identity(anchor), _process_identity(node)
    if recorded is None or observed is None:
        return False
    try:
        return recorded[1] == observed[1] == native_boot_id() and recorded[0].same_birth(
            observed[0]
        )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return False


def classify_anchor(anchor: dict[str, Any]) -> str:
    """Strict liveness for one recorded anchor: alive | dead | reused | denied | unknown.

    Unreadable evidence is not exit or PID reuse. Both caller attestation and
    the supervisor use this classification; neither substitutes Linux wall time.
    """
    recorded = _process_identity(anchor)
    if recorded is None:
        return "unknown"
    identity, boot = recorded
    try:
        current_boot = native_boot_id()
        if not _valid_boot_id(current_boot):
            return "unknown"
        if boot != current_boot:
            return "dead"
        observed = OwnedProcess.capture(psutil.Process(identity.pid))
        if not identity.same_birth(observed):
            return "reused"
        return "alive" if observed.live() else "dead"
    except psutil.NoSuchProcess:
        return "dead"
    except psutil.AccessDenied:
        return "denied"
    except (psutil.Error, OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return "unknown"


def provider_anchor_states(process_metadata: object) -> list[str]:
    """Classify each recorded provider anchor — the supervisor's liveness view."""
    return [
        classify_anchor(node)
        for node in _metadata_nodes(process_metadata)
        if _is_provider_node(node)
    ]


def verify_caller(lease: dict[str, Any], caller: object) -> None:
    """Require the caller to descend from the lease's recorded controller process.

    The session id is the only control credential (user ruling 2026-09-16);
    this presence check replaces the deliverable token: a recorded provider
    anchor (codex / claude — claude's native ``claude/versions/<version>``
    layout included) must appear among the caller's live ancestors with
    the same native birth and boot identity. Linux requires kernel start ticks;
    other platforms compare exact native timestamps. Every failure is fail-closed
    and classified as no-anchor, anchor-dead, anchor-unavailable, or chain-mismatch.
    The operator gets a next step instead of a dead end. An orphaned session ends on the
    native side or by TTL; it is never re-anchored here.
    """
    recorded = _metadata_nodes(lease.get("process_metadata"))
    anchors = [node for node in recorded if _is_provider_node(node)]
    if not anchors:
        raise ImpersonationError(
            "Controller caller check failed (no-anchor): this session records no "
            "provider controller process to attest against. End it from the native "
            "side (restart/stop) or let its TTL expire."
        )
    live = _metadata_nodes(caller)
    if any(_same_process(anchor, node) for anchor in anchors for node in live):
        return
    states = {classify_anchor(anchor) for anchor in anchors}
    if states & {"alive"}:
        raise ImpersonationError(
            "Controller caller check failed (chain-mismatch): the recorded "
            "controller process is alive but this caller does not descend from it. "
            "Run impersonation commands from the controller session."
        )
    if states & {"unknown", "denied"}:
        raise ImpersonationError(
            "Controller caller check failed (anchor-unavailable): the recorded "
            "controller's native identity cannot be verified. End the session "
            "from the native side (restart/stop) or let its TTL expire."
        )
    if "reused" in states:
        raise ImpersonationError(
            "Controller caller check failed (anchor-dead): the recorded controller "
            "process is gone (its pid now belongs to a different process). End the "
            "session from the native side (restart/stop) or let its TTL expire."
        )
    raise ImpersonationError(
        "Controller caller check failed (anchor-dead): the recorded controller "
        "process is gone. End the session from the native side (restart/stop) or "
        "let its TTL expire."
    )


def authenticate(lease: dict[str, Any], caller: object) -> None:
    """Attested controller authority: caller presence plus same-machine placement."""
    verify_caller(lease, caller)
    local(lease)


def require_native(conn: psycopg.Connection, incarnation: RuntimeIncarnation) -> dict[str, Any]:
    meta = lock_agent(conn, incarnation.agent_id)
    fresh = conn.execute(
        "SELECT lease_expires_at > clock_timestamp() FROM agents_meta WHERE id=%s",
        (incarnation.agent_id,),
    ).fetchone()
    if (
        (meta["runtime_generation"], meta["runtime_owner"])
        != (incarnation.generation, incarnation.owner)
        or meta["status"] not in ("running", "idling")
        or fresh != (True,)
    ):
        raise ImpersonationError("Native runtime no longer owns this agent")
    return meta


def insert_handoff(
    conn: psycopg.Connection, lease: dict[str, Any], content: str, *, expired: bool = False
) -> int:
    """Write only the negotiated workflow's handoff, in the lease transaction.

    The native consumer has explicitly accepted this source through its private
    request table. This is not generic caller-protocol activation: all ordinary
    external message/lifecycle writers keep their existing rollout fences.
    """
    source = "system:impersonation" if expired else lease["source"]
    payload = caller_payload(source, {"impersonation_id": str(lease["id"])})
    row = conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source,payload) "
        "VALUES(%s,%s,'chat',%s,%s) RETURNING id",
        (lease["agent_id"], content, source, Jsonb(payload)),
    ).fetchone()
    if row is None:
        raise RuntimeError("Handoff INSERT returned no row")
    return row[0]


def dismiss_reminders(conn: psycopg.Connection, lease: dict[str, Any]) -> None:
    """Retire this lease's pending renewal reminders, in the lease transaction.

    Reminders are written for the external controller only; once the lease
    ends (release or expiry) they are meaningless to the native inbox. ACKed
    rows are already done. The payload key matches the maintenance insert.
    """
    conn.execute(
        "UPDATE inbound_messages SET status='done' "
        "WHERE agent_id=%s AND kind='reminder' AND status='pending' "
        "AND payload->>'lease_id'=%s",
        (lease["agent_id"], str(lease["id"])),
    )


def expire(conn: psycopg.Connection, lease: dict[str, Any]) -> dict[str, Any]:
    if lease["status"] not in OPEN:
        return lease
    fresh = conn.execute("SELECT %s > clock_timestamp()", (lease["expires_at"],)).fetchone()
    # Reconcile across the entire lease, not just the relay's bounded inbox
    # page. ACK and delivery reservations take this same agent/lease lock.
    overdue = (
        conn.execute(
            "SELECT m.inbound_id FROM agent_impersonation_messages m "
            "JOIN inbound_messages i ON i.id=m.inbound_id "
            "WHERE m.lease_id=%s AND m.acknowledged_at IS NULL AND i.status='pending' "
            "AND m.delivery_attempts >= %s "
            "AND m.last_delivery_at <= clock_timestamp() - %s*interval '1 second' "
            "ORDER BY m.inbound_id LIMIT 1",
            (lease["id"], lease["max_delivery_attempts"], lease["ack_window_seconds"]),
        ).fetchone()
        if lease["status"] == "active"
        else None
    )
    if fresh == (True,) and overdue is None:
        return lease
    from shared.agents.impersonation_manifest import close_manifest_admission, is_protocol_v1

    if is_protocol_v1(lease):
        close_manifest_admission(conn, str(lease["id"]))
    detail = (
        f"the executor did not ACK message {overdue[0]} after "
        f"{lease['max_delivery_attempts']} delivery attempts "
        f"({lease['ack_window_seconds']}s per ACK window)"
        if overdue is not None
        else None
    )
    inbound_id = None
    if lease["status"] == "active" and not lease["automatic"]:
        inbound_id = insert_handoff(
            conn,
            lease,
            f"Impersonation {lease['id']} by {lease['source']} "
            + (f"stopped: {detail}. " if detail else "expired. ")
            + "Control has returned. Unacknowledged messages remain pending; "
            "no external completion summary was supplied.",
            expired=True,
        )
    conn.execute(
        "UPDATE agent_impersonations SET status='expired',ended_at=clock_timestamp(), "
        "summary_inbound_id=%s,rejection_reason=COALESCE(%s,rejection_reason) WHERE id=%s",
        (inbound_id, f"aborted: {detail}" if detail else None, lease["id"]),
    )
    # A pending renewal reminder only matters to the external session; the
    # lease is over, so it must never reach the native agent's inbox.
    dismiss_reminders(conn, lease)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM agent_impersonations WHERE id=%s", (lease["id"],))
        refreshed = cur.fetchone()
        assert refreshed is not None  # noqa: S101 - locked session exists
        lease = refreshed
    lease["status"] = "expired"
    lease["summary_inbound_id"] = inbound_id
    return lease


def validate_active(
    lease: dict[str, Any], caller: object, *, fresh: bool, machine: str, status: str
) -> None:
    """Validate either a joined read snapshot or rows already locked for mutation."""
    if lease["status"] != "active" or not fresh:
        raise ImpersonationError(
            "Impersonation session is not active (stale-session): it ended or its "
            "TTL expired; the native agent continues."
        )
    if machine != lease["machine"] or status not in ("running", "idling"):
        raise ImpersonationError("Agent placement or lifecycle changed")
    authenticate(lease, caller)


def require_active_locked(conn: psycopg.Connection, lease: dict[str, Any], caller: object) -> None:
    # Do not persist expiration here and then raise (which would roll it back).
    # The native boundary/get reconciler persists it independently.
    meta = lock_agent(conn, lease["agent_id"])
    fresh = conn.execute("SELECT %s > clock_timestamp()", (lease["expires_at"],)).fetchone()
    validate_active(
        lease, caller, fresh=fresh == (True,), machine=meta["machine"], status=meta["status"]
    )


RELAY_PROVIDERS = ("codex", "claude")


def validate_relay_spec(
    provider: str | None, thread_id: str | None, codex_remote: str | None
) -> None:
    """The request must name its relay endpoint up front; the native side
    never guesses one. A codex remote must be a unix:// or ws:// address,
    rejected here so a malformed endpoint fails the request instead of the
    relay (review N4)."""
    if provider not in RELAY_PROVIDERS:
        raise ValueError("Relay provider must be 'codex' or 'claude'")
    if provider == "codex":
        if not thread_id:
            raise ValueError("Codex relay requires the existing session's thread id")
        if codex_remote is not None and not codex_remote.startswith(("unix://", "ws://")):
            raise ValueError("Codex remote must be a unix:// or ws:// endpoint")
    elif thread_id is not None or codex_remote is not None:
        raise ValueError("Claude relay routes to its owner; thread id and remote are rejected")


def authenticate_relay(lease: dict[str, Any], relay_token: str) -> None:
    """The relay's scoped credential: inbox/delivery/beat, never controller authority."""
    if lease["relay_token_hash"] is None or not hmac.compare_digest(
        lease["relay_token_hash"], token_hash(relay_token)
    ):
        raise ImpersonationError("Invalid relay token")
    local(lease)


def require_relay_active_locked(
    conn: psycopg.Connection, lease: dict[str, Any], relay_token: str
) -> None:
    meta = lock_agent(conn, lease["agent_id"])
    fresh = conn.execute("SELECT %s > clock_timestamp()", (lease["expires_at"],)).fetchone()
    authenticate_relay(lease, relay_token)
    if lease["status"] != "active" or fresh != (True,):
        raise ImpersonationError("Impersonation is not active or its TTL has expired")
    if meta["machine"] != lease["machine"] or meta["status"] not in ("running", "idling"):
        raise ImpersonationError("Agent placement or lifecycle changed")

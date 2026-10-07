"""Agent identity for host turns, exec children and launched scripts.

The identity of a process is the `AgentIdentity` of the `AvaContext` it is bound to
(`ava.sdk_surface.process_context`); this module reads it and holds no state. The agent host binds a turn contextvar
(`base/native_process/turn_identity.py`) around execution and never establishes one process-wide
agent id for its many agents. The exec child binds a context built from its request envelope, a
script an agent launched derives one from AVA_AGENT_ID, and an external controller binds one
carrying its lease.
Reads resolve an explicitly borrowed external identity first (with lease validation), then the
turn contextvar, then the bound context.

The canonical identity is framework-internal; `ava.self.AGENT_ID` re-exports
it. Disabling the agent-facing `self` namespace does not remove identity from
framework callers. `owns_loop` authorizes lifecycle self-actions in a turn or
its exec child; background scripts carry it False so they cannot compact or restart the agent
whose identity they carry.

This module carries no agent-facing help surface. Cluster configuration and
credentials remain separate from the agent identity.
"""

from __future__ import annotations

from ava.sdk_surface import process_context
from base.agents.context.identity import AgentIdentity
from base.native_process.turn_identity import current_turn_agent_id


def _bound() -> AgentIdentity | None:
    """The identity of the bound context, or None when no context is bound."""
    context = process_context.peek()
    return None if context is None else context.identity


def validate_external_identity() -> int | None:
    """Recheck an attached lease; no-op for the existing native runtime paths."""
    identity = _bound()
    return identity.lease.validate() if identity is not None and identity.lease else None


def is_launched_child() -> bool:
    """True in a process an agent launched — a watcher / persistent-shell /
    schedule child that carries the agent's `AVA_AGENT_ID` in its environment
    but does not own the turn loop. False in the agent process itself
    (`owns_loop` is True) and in gateway / cli / ad-hoc processes (no
    `AVA_AGENT_ID`, so no context binds).

    Binds the context from the environment first (so a fresh child that has not
    touched `agent_identity` yet reports correctly), then reports the launched-child
    signal. This gates the lazy plugin-namespace load in `ava.__getattr__`: only
    such a child self-loads plugins on first unknown-attribute access — so a bare
    `python x.py` in a persistent shell session gets `ava.tasks` et al. without a
    bootstrap, while the agent process (which loaded plugins explicitly) and
    gateway / cli keep fail-fast on a genuinely unknown `ava.X`. A bound turn
    context is the hosted runner itself — never a launched child."""
    if current_turn_agent_id() is not None:
        return False
    identity = _bound()
    return identity is not None and identity.agent_id is not None and not identity.owns_loop


def agent_id() -> int | None:
    """Resolve the agent id used to attribute this process's work.

    A validated borrowed identity takes precedence, followed by the hosted turn
    context and the bound context's identity. An invalid borrowed lease raises
    instead of falling back. Returns `None` when no source provides an identity;
    callers that tolerate this pre-bootstrap state must check for it explicitly.
    """
    external = validate_external_identity()
    if external is not None:
        return external
    turn = current_turn_agent_id()
    if turn is not None:
        return turn
    identity = _bound()
    return None if identity is None else identity.agent_id


def require_agent_id() -> int:
    """Return this process's agent id, or raise if it has none.

    Use this at every call site that stamps the agent id into durable data
    (spawner / message source / etc.) — it fails fast instead of letting
    ``None`` leak into the database as the malformed string ``"agent:None"``.

    Raises:
        RuntimeError: no context carrying an agent id is bound and
        ``AVA_AGENT_ID`` is not set in the environment.
    """
    resolved = agent_id()
    if resolved is None:
        raise RuntimeError(
            "this process has no established agent identity — "
            "ava.agents.spawn / send_message / get_last_message require "
            "a bootstrapped agent process, or AVA_AGENT_ID in the environment "
            "of a shell session launched by one"
        )
    return resolved


def require_lease_free_agent_id() -> int:
    """Require an agent context without a borrowed lease for remote admission.

    A live ExternalLease is an in-process callback, not an HTTP credential or
    serializable authority. Remote compound acceptance cannot revalidate it
    after its own transaction lock wait. This guard makes no server ACL claim.
    """
    identity = _bound()
    if identity is not None and identity.lease is not None:
        raise ValueError("strong task assignment does not support a borrowed lease")
    actor = require_agent_id()
    if isinstance(actor, bool) or not isinstance(actor, int):
        raise TypeError("actor agent id must be an integer")
    if actor <= 0:
        raise ValueError("actor agent id must be positive")
    return actor


def require_actor() -> str:
    """Return this process's asserted provenance, validating a borrowed lease first.

    A borrowed `agent:<id>` identity takes precedence, followed by a hosted turn,
    an explicit external tool profile, a non-agent actor of the bound context,
    and its agent identity. An invalid borrowed lease
    raises instead of falling back. These provenance channels do not replace
    the gateway's credential checks.

    Use at every call site that stamps provenance into durable data (spawner /
    message source). Fails fast if neither a system actor nor an agent id was
    established, instead of letting a malformed ``agent:None`` leak into the DB.

    Raises:
        RuntimeError: no actor or agent identity is available, or the borrowed
            lease no longer permits this identity.
    """
    borrowed = validate_external_identity()
    if borrowed is not None:
        return f"agent:{borrowed}"
    turn = current_turn_agent_id()
    if turn is not None:
        return f"agent:{turn}"
    from base.agents.messages.external_caller import external_caller

    external = external_caller()
    if external is not None:
        return external.source()
    identity = _bound()
    if identity is not None and identity.actor is not None:
        return identity.actor
    if identity is None or identity.agent_id is None:
        raise RuntimeError(
            "this process has no established actor or agent identity — "
            "ava.agents.spawn / send_message / resurrect need one "
            "(a bootstrapped agent process, or a context with an actor for a system principal)"
        )
    return f"agent:{identity.agent_id}"


def default_actor() -> str:
    """Provenance principal for the *default*-source paths (terminate / restart /
    resurrect when the caller passes no source). Same as `require_actor` but
    tolerant of absent identity: with none it returns the pre-actor
    ``agent:None`` sentinel, preserving the legacy default-source behavior rather
    than turning an unset identity into an error at these lower-stakes sites.
    An invalid borrowed lease still raises instead of falling back."""
    borrowed = validate_external_identity()
    if borrowed is not None:
        return f"agent:{borrowed}"
    turn = current_turn_agent_id()
    if turn is not None:
        return f"agent:{turn}"
    from base.agents.messages.external_caller import external_caller

    external = external_caller()
    if external is not None:
        return external.source()
    identity = _bound()
    if identity is not None and identity.actor is not None:
        return identity.actor
    return f"agent:{None if identity is None else identity.agent_id}"


def assert_self_action(action: str) -> None:
    """Refuse a lifecycle self-action unless this process is a bootstrapped agent
    process — one that both owns the turn loop and has an established identity.

    The identity check fails fast at the source rather than letting an INSERT with
    a null agent_id defer the failure to a DB constraint violation (the case of
    `ava` imported without a bootstrap, e.g. an ad-hoc `python -c`).

    Raises:
        RuntimeError: this process does not own the agent turn loop (a launched
            background script), or has no established identity.
    """
    if current_turn_agent_id() is not None:
        # A hosted turn context: the runner drives this agent's loop, so the
        # turn is the loop owner by construction.
        return
    identity = _bound()
    if identity is not None and not identity.owns_loop:
        raise RuntimeError(
            f"ava.self.{action}() can only be called from inside an agent process, "
            f"not from a background script launched by one"
        )
    if identity is None or identity.agent_id is None:
        raise RuntimeError(
            f"ava.self.{action}() needs an established agent identity; this process "
            f"never ran the agent bootstrap (agent id is unset)"
        )

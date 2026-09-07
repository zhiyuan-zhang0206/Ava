"""Agent identity for host turns, exec children and launched scripts.

The agent host binds a turn contextvar (`shared/turn_identity.py`) around
execution. It never establishes one process-wide agent id for its many agents.
A disposable exec child or launched script may call `establish` once, and
shell-launched children can derive the same identity from AVA_AGENT_ID.
Reads resolve an explicitly borrowed external identity first (with lease
validation), then the turn contextvar, then the process slot or child env.

The canonical identity is framework-internal; `ava.self.AGENT_ID` re-exports
it. Disabling the agent-facing `self` namespace does not remove identity from
framework callers. `owns_loop` authorizes lifecycle self-actions in a turn or
its exec child; background scripts establish with owns_loop=False so they
cannot compact or restart the agent whose identity they carry.

This module carries no agent-facing help surface. Cluster configuration and
credentials remain separate from the agent identity.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from shared.turn_identity import current_turn_agent_id

# This process's agent id — the single framework-internal source of truth, set
# once by `establish`. `None` (not `0`) is the pre-bootstrap placeholder: a real
# agent id is always >= 1 (auto-assigned, 1-based), so `0` sits inside the
# legal-looking id space and a DB-direct write that read it before bootstrap
# would land a positive-looking `agent_id=0` ghost row; `None` is outside the id
# space, so the same premature write hits a NOT NULL `agent_id` column and raises
# at the source instead.
_agent_id: int | None = None

# Mutable per-process flag (lowercase on purpose — runtime state, not a
# constant): True for the host/exec execution path. Default True so host
# turns and the test harness —
# neither of which goes through a launched-script bootstrap — keep the unguarded
# behavior; a launched script flips it False via `establish(..., owns_loop=False)`,
# the one context where the lifecycle self-actions must refuse.
_owns_loop: bool = True

# The provenance principal this process acts as — the string stamped as
# `spawner` / message `source` on work it originates (e.g. `schedule:7`). `None`
# means "no non-agent principal set": provenance then derives from the agent id
# (`agent:<id>`). A non-agent process (a gateway-hosted schedule) that has no
# agent id calls `establish_actor` to set this so `ava.agents.*` can attribute
# its spawns/messages without a malformed `agent:None`.
_actor: str | None = None

# An explicitly attached external controller revalidates its borrowed identity
# at every SDK read. Unset in native runtimes and ordinary launched children.
_external_identity: Callable[[], int] | None = None


def validate_external_identity() -> int | None:
    """Recheck an attached lease; no-op for the existing native runtime paths."""
    return _external_identity() if _external_identity is not None else None


def establish(agent_id: int, *, owns_loop: bool) -> None:
    """Bind this process's identity. Sets the framework-internal agent id (read
    via `agent_id()`, re-exported to the agent as `ava.self.AGENT_ID`) and
    records whether this process owns the agent turn loop. Called once at
    process startup."""
    global _agent_id, _owns_loop  # noqa: PLW0603 — single per-process bootstrap slot
    _agent_id = agent_id
    _owns_loop = owns_loop


def establish_actor(actor: str) -> None:
    """Bind a non-agent provenance principal for this process (e.g.
    `"schedule:7"`). Sets the actor string that `require_actor` / `default_actor`
    return, so `ava.agents.spawn / send_message / resurrect` attribute this
    process's work to a named principal that is not an agent. Called once at
    startup by a gateway-hosted schedule's runner, which has no agent id."""
    global _actor  # noqa: PLW0603 — single per-process bootstrap slot
    _actor = actor


def _try_establish_from_env() -> None:
    """If no agent identity is established, try to read ``AVA_AGENT_ID`` from
    the environment and establish one with ``owns_loop=False``.

    The session env allowlist deliberately does NOT carry ``AVA_AGENT_ID``
    (agent-scope, Task #856 / audit F-s3-4) — so this path fires only in a
    process whose launcher set the var some other way: a watcher / schedule
    child, whose generated bootstrap inlines it (`ava.watcher._build_boot`),
    or a test harness. A bare ``python x.py`` in a persistent shell session
    has no identity and reports it as such. ``owns_loop=False`` means
    lifecycle self-actions (``ava.self.compact/terminate/restart``)
    are refused — only the real agent process owns the turn loop.
    """
    global _agent_id, _owns_loop  # noqa: PLW0603
    if _agent_id is not None:
        return
    env_id = os.environ.get("AVA_AGENT_ID")
    if env_id is not None:
        _agent_id = int(env_id)
        _owns_loop = False


def is_launched_child() -> bool:
    """True in a process an agent launched — a watcher / persistent-shell /
    schedule child that carries the agent's `AVA_AGENT_ID` in its environment
    but does not own the turn loop. False in the agent process itself
    (`owns_loop` is True) and in gateway / cli / ad-hoc processes (no
    `AVA_AGENT_ID`, so no identity establishes).

    Establishes from the environment first (so a fresh child that has not
    touched `_boot` yet reports correctly), then reports the launched-child
    signal. This gates the lazy plugin-namespace load in `ava.__getattr__`: only
    such a child self-loads plugins on first unknown-attribute access — so a bare
    `python x.py` in a persistent shell session gets `ava.tasks` et al. without a
    bootstrap, while the agent process (which loaded plugins explicitly) and
    gateway / cli keep fail-fast on a genuinely unknown `ava.X`. A bound turn
    context is the hosted runner itself — never a launched child."""
    if current_turn_agent_id() is not None:
        return False
    _try_establish_from_env()
    return _agent_id is not None and not _owns_loop


def agent_id() -> int:
    """Resolve the agent id used to attribute this process's work.

    A validated borrowed identity takes precedence, followed by the hosted turn
    context, the process identity bound by `establish`, and `AVA_AGENT_ID` from
    the environment. An invalid borrowed lease raises instead of falling back.
    Returns `None` when no source provides an identity; callers that tolerate
    this pre-bootstrap state must check for it explicitly.
    """
    external = validate_external_identity()
    if external is not None:
        return external
    turn = current_turn_agent_id()
    if turn is not None:
        return turn
    _try_establish_from_env()
    return _agent_id  # type: ignore[return-value]


def require_agent_id() -> int:
    """Return this process's agent id, or raise if never established.

    Use this at every call site that stamps the agent id into durable data
    (spawner / message source / etc.) — it fails fast instead of letting
    ``None`` leak into the database as the malformed string ``"agent:None"``.

    Raises:
        RuntimeError: ``establish`` was never called and ``AVA_AGENT_ID`` is
        not set in the environment.
    """
    external = validate_external_identity()
    if external is not None:
        return external
    turn = current_turn_agent_id()
    if turn is not None:
        return turn
    _try_establish_from_env()
    if _agent_id is None:
        raise RuntimeError(
            "this process has no established agent identity — "
            "ava.agents.spawn / send_message / get_last_message require "
            "a bootstrapped agent process, or AVA_AGENT_ID in the environment "
            "of a shell session launched by one"
        )
    return _agent_id


def require_actor() -> str:
    """Return this process's asserted provenance, validating a borrowed lease first.

    A borrowed `agent:<id>` identity takes precedence, followed by a hosted turn,
    an explicit external tool profile, a non-agent actor from `establish_actor`,
    and the process or environment agent identity. An invalid borrowed lease
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
    from shared.external_caller import external_caller

    external = external_caller()
    if external is not None:
        return external.source()
    _try_establish_from_env()
    if _actor is not None:
        return _actor
    if _agent_id is None:
        raise RuntimeError(
            "this process has no established actor or agent identity — "
            "ava.agents.spawn / send_message / resurrect need one "
            "(a bootstrapped agent process, or establish_actor for a system principal)"
        )
    return f"agent:{_agent_id}"


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
    from shared.external_caller import external_caller

    external = external_caller()
    if external is not None:
        return external.source()
    _try_establish_from_env()
    if _actor is not None:
        return _actor
    return f"agent:{_agent_id}"


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
    if not _owns_loop:
        raise RuntimeError(
            f"ava.self.{action}() can only be called from inside an agent process, "
            f"not from a background script launched by one"
        )
    if _agent_id is None:
        raise RuntimeError(
            f"ava.self.{action}() needs an established agent identity; this process "
            f"never ran the agent bootstrap (agent id is unset)"
        )

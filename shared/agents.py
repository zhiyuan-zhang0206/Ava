"""Cross-process data contract for the agent gateway: status enum + exceptions + error-wire protocol.

This module only defines types, no impl — agent process and gateway
communicate over HTTP; both ends must see the same definitions of
status / exception / wire-level reason.

- Impl (spawn / resurrection / fork-checkpoint copy) lives in
  `ops/agents.py`; gateway routes and local ops call it.
- On the agent process side, the SDK (`ava.agents.*`) uniformly goes
  through HTTP to call gateway routes — no longer imports impl from
  this module, only imports types + exceptions to catch.

Error wire protocol:
- Gateway side catches `AvaAgentError` subclasses -> response body
  `{"detail": str(exc), "reason": exc.reason}` + `exc.http_status`
- SDK side parses the response's `reason` field -> looks up
  `EXCEPTION_BY_REASON` to reconstruct the same exception type and
  raises to the caller (preserving the original message)
- `reason` is the wire string contract; a new error = new enum value +
  new exception class; both ends sync by importing. `tests/
  test_agent_error_wire_equivalence.py` parametrizes
  EXCEPTION_BY_REASON.values() to lock the end-to-end loop.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar


class AgentStatus(StrEnum):
    RUNNING = "running"
    IDLING = "idling"
    RESTARTING = "restarting"
    TERMINATED = "terminated"


class TerminationSource(StrEnum):
    """WHO/WHAT wrote `agents_meta.status='terminated'` — stamped by EVERY code path
    that writes it, and the sole input to "may this corpse be auto-resurrected".

    Meaningful only while status='terminated'; cleared to NULL on the
    terminated -> idling resurrect transition, so the mark is per-death.

    The write sites embed these as SQL literals (a terminated-write stamps status
    and source in ONE statement, so the pair can never come apart); this enum is
    the value-set source of truth, locked to the column's CHECK by
    `tests/test_db_check_enum_sync.py` and to the literals by
    `scripts/lint_termination_source.py`.

    Resurrect policy — INVOLUNTARY deaths come back, intentional ones stay dead:
    `ops/controllers/resurrect.py` allowlists exactly `RESURRECTABLE`.
    """

    # Intentional — the user's will (force-kill, or a terminate that found the pid
    # already dead). Never auto-resurrected.
    USER = "user"
    # Intentional — the agent's own graceful process-exit finalize (self-terminate,
    # or a caught SIGTERM/SIGHUP that ran the exit finally). Never auto-resurrected.
    # Also records a positively exited target after its restart deadline fails;
    # the command retains that failure separately, never a successful observation.
    EXIT = "exit"
    # Involuntary — a restarter corpse reaper found a dead pid / stale unclaimed idling
    # row (OOM/SIGKILL/crash leaves no finally, so it never reaches EXIT).
    REAPER = "reaper"
    # Involuntary — a launch that never confirmed: the launcher's confirm poll timed
    # out (`ops/agent_launch.py`), or the child's own early-boot gates rejected the
    # boot before claiming its row (`agent/_starting.py` — schema mismatch or
    # placement mismatch). Both are "the wake never came up", and both self-heal
    # once the cause clears, so both are resurrect-eligible (backoff-spaced).
    LAUNCH_CONFIRM = "launch-confirm"
    # A framework-detected inconsistency in the row's OWN state killed it — not a
    # death anyone requested and not a launch that can be retried
    # (`ops/agents.py:respawn_agent` finding status='restarting' with no 'restart'
    # inbound). Deliberately NOT resurrectable: the row's history is corrupt, so an
    # automatic retry would convert a loud one-time fault into a recurring
    # background warning. Ops inspects, then resurrects by hand.
    INTEGRITY = "integrity"

    @classmethod
    def resurrectable(cls) -> tuple[TerminationSource, ...]:
        """The sources that mean "died involuntarily, bring it back" — the
        allowlist `CrashResurrectController` filters its scan on. NULL (a
        pre-column legacy row) is outside this enum and likewise never eligible."""
        return (cls.REAPER, cls.LAUNCH_CONFIRM)


class TerminateResult(StrEnum):
    ENQUEUED = "enqueued"
    ALREADY_TERMINATED = "already_terminated"


class RestartResult(StrEnum):
    ENQUEUED = "enqueued"
    ALREADY_TERMINATED = "already_terminated"


class ResurrectResult(StrEnum):
    SPAWNED = "spawned"
    ALREADY_ALIVE = "already_alive"


class ErrorReason(StrEnum):
    """Error identifiers on the SDK <-> Gateway HTTP wire.

    The response body's `reason` field uses these values; SDK looks up
    EXCEPTION_BY_REASON to reconstruct the matching exception type and
    raise to the caller. New wire errors must simultaneously add an
    enum value + an AvaAgentError subclass.

    **Not all ResurrectError / ForkError subclasses go on the wire** —
    `ResurrectAlreadyAlive` is caught locally by the resurrect
    endpoint and turned into an idempotent 200 + `status='already_alive'`;
    the SDK returns `ResurrectResult.ALREADY_ALIVE` from that status string
    (it never raises the exception) and does not take the reason channel.
    So it is not an AvaAgentError subclass and is not in this enum.
    """

    AGENT_NOT_FOUND = "agent_not_found"
    FORK_SOURCE_EMPTY = "fork_source_empty"
    FORK_CHECKPOINT_NOT_FOUND = "fork_checkpoint_not_found"
    MACHINE_NOT_REGISTERED = "machine_not_registered"
    SPAWN_TARGET_NOT_AGENT_RUNNER = "spawn_target_not_agent_runner"
    MACHINE_PAUSED = "machine_paused"
    CROSS_MACHINE_GATEWAY_UNAVAILABLE = "cross_machine_gateway_unavailable"
    INDEXER_UNAVAILABLE = "indexer_unavailable"
    CHANNEL_NOT_CONFIGURED = "channel_not_configured"
    INVALID_MODEL_CONFIG = "invalid_model_config"


# Reverse lookup table from wire reason -> exception class; used by
# SDK `_raise_from_response`. `AvaAgentError.__init_subclass__` writes
# entries automatically at class-declaration time; do **not** maintain
# by hand — adding a new error only requires "add enum + add class
# with reason/http_status", and registration follows on import.
# tests/test_agent_error_wire_equivalence.py parametrizes this dict to
# lock the end-to-end loop; the assertion at the module end catches
# "enum added but class missing" in the reverse direction.
EXCEPTION_BY_REASON: dict[ErrorReason, type[AvaAgentError]] = {}


class AvaAgentError(Exception):
    """Root of **wire-encoded** agent gateway errors — the
    gateway uses one `@app.exception_handler(AvaAgentError)` to map to
    an HTTP response, and the SDK looks up `EXCEPTION_BY_REASON` to
    reconstruct.

    Concrete subclasses must set `reason: ClassVar[ErrorReason]` +
    `http_status: ClassVar[int]`. Marker parent classes like
    `ResurrectError` / `ForkError` are not subclasses — they mark
    catch groups and do not carry wire fields (concrete subclasses use
    multiple inheritance to inherit marker + AvaAgentError together).

    `__init_subclass__` enforces ClassVars at class-declaration time +
    auto-registers into `EXCEPTION_BY_REASON` — single source of
    truth; adding a new error has only two steps "add enum + add
    class with the two ClassVars", and registration follows.
    """

    reason: ClassVar[ErrorReason]
    http_status: ClassVar[int]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # `reason` / `http_status` are wire contracts; if a subclass
        # forgets to set them = the wire protocol has a hole. Fail at
        # import time, do not wait until first raise to AttributeError.
        # Use `cls.__dict__` rather than `hasattr` to prevent "subclass
        # inherits a parent ClassVar placeholder declaration" from
        # being misjudged as already set.
        if "reason" not in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} inheriting AvaAgentError must set `reason: ClassVar[ErrorReason]`"
            )
        if "http_status" not in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} inheriting AvaAgentError must set `http_status: ClassVar[int]`"
            )
        EXCEPTION_BY_REASON[cls.reason] = cls


# Wire note: AgentNotFound is wire-encoded and re-exported as `ava.agents.AgentNotFound`.
# ResurrectAlreadyAlive is NOT wire-encoded — the resurrect endpoint locally turns
# it into an idempotent 200 status='already_alive'. The dedicated resurrect endpoint
# (POST /api/agents/{id}/resurrect) is retained — the frontend button + SDK use it;
# auto-resurrect in the chat-delivery path additionally covers the "no message" case.
class ResurrectError(Exception): ...


class ForkError(Exception):
    """Marker parent class for exceptions `spawn(fork_from=...)` may
    raise — for broad catch.

    Concrete subclasses: `ForkSourceEmpty` (source has no checkpoint) /
    `ForkCheckpointNotFound` (explicit checkpoint id does not exist on
    source). Both wire-encoded. The SDK re-exports as `ava.agents.X`.
    """


class AgentNotFound(ResurrectError, AvaAgentError):  # noqa: N818 — style consistent with FileNotFoundError
    reason = ErrorReason.AGENT_NOT_FOUND
    http_status = 404


# Not an AvaAgentError subclass — the resurrect endpoint catches locally and
# returns 200 {"status":"already_alive"} (idempotent UX); the SDK client maps
# that string to ResurrectResult.ALREADY_ALIVE (it never raises this exception),
# so the path never takes the wire reason / handler.
class ResurrectAlreadyAlive(ResurrectError): ...  # noqa: N818


class ResurrectBudgetExhausted(ResurrectError): ...  # noqa: N818


class ForkSourceEmpty(ForkError, AvaAgentError):  # noqa: N818 — style consistent with ENOENT
    """Source agent has no checkpoint — cannot fork.

    Usually the source agent has not run any code yet (after spawn it
    is idling, waiting for inbound, has not entered LLM/exec). Fork
    after the source has at least one graph step.
    """

    reason = ErrorReason.FORK_SOURCE_EMPTY
    http_status = 409


class ForkCheckpointNotFound(ForkError, AvaAgentError):  # noqa: N818
    """The `fork_checkpoint` id does not exist on the source agent —
    typo / already cleaned / crossed agents. The SDK's internal
    resolve-latest does not trigger this; only manual ckpt id does.
    """

    reason = ErrorReason.FORK_CHECKPOINT_NOT_FOUND
    http_status = 409


class GatewayUnavailable(Exception):  # noqa: N818 — state description, no Error suffix, same style as AgentNotFound
    """SDK-only: gateway HTTP unreachable (connect / read / pool /
    write timeout, connection refused, DNS failure, protocol error,
    etc.).

    Ava is localhost-only; gateway is a hard dependency of the agent
    gateway and must be up before spawn / resurrect /
    send_message. This does not go into
    `EXCEPTION_BY_REASON` — it never appears in a gateway response
    body (the response never arrives). SDK `_gateway_client` raises
    this to the caller on the `httpx.TransportError` family (general
    network-layer failure); the agent decides whether to retry / exit.
    """


class MachineNotRegistered(AvaAgentError):  # noqa: N818 — state description, same style as AgentNotFound
    """During cross-machine spawn the target machine has not registered
    itself yet, or the machine name is misspelled.

    Gateway-side `gateway/agents.py:spawn_agent` raises when
    `shared/machines.py:lookup` finds no row; propagated to the SDK
    via the wire protocol.
    """

    reason = ErrorReason.MACHINE_NOT_REGISTERED
    http_status = 404


class SpawnTargetNotAgentRunner(AvaAgentError):  # noqa: N818 — state description, same style as AgentNotFound
    """The spawn target machine is registered but does not carry the
    agent-runner capability, so it cannot run agents (e.g. a gateway-only
    host).

    Pass a machine that runs agents — see `ava.agents.list_machines()`.
    """

    reason = ErrorReason.SPAWN_TARGET_NOT_AGENT_RUNNER
    http_status = 400


class MachinePaused(AvaAgentError):  # noqa: N818 — state description, same style as AgentNotFound
    """The spawn target machine is registered but is currently PAUSED
    (`ava cluster pause` — operator-set latch, migration 20260814T182039):
    it is temporarily pulled out of the cluster (e.g. the owner is away for a
    week and disconnected it), so its ops server may be unreachable and its
    agents have been terminated.

    Resume it first (`ava cluster resume <name>` on the gateway), then spawn.
    """

    reason = ErrorReason.MACHINE_PAUSED
    http_status = 409


# Cluster routing (multihost): SDK calls from an agent-runner target the
# gateway directly (no local gateway on agent-runner); transport-layer
# unreachability surfaces as CrossMachineGatewayUnavailable. IndexerUnavailable
# is the gateway *backend* (gemini embedding / milvus) being truly unreachable.
# Kept as a comment, not docstring — the docstring renders into every agent's
# SDK docs, where this cross-machine routing note would be noise.
class IndexerUnavailable(AvaAgentError):  # noqa: N818
    """The semantic-search backend is unreachable; the notes themselves
    are still readable as plain files."""

    reason = ErrorReason.INDEXER_UNAVAILABLE
    http_status = 503


class ChannelNotConfigured(AvaAgentError):  # noqa: N818 — state description, no Error suffix, same style as AgentNotFound
    """The push channel is not configured or its backend is unavailable.

    Decide for yourself whether to fall back to another notification path.
    """

    reason = ErrorReason.CHANNEL_NOT_CONFIGURED
    http_status = 503


class CrossMachineGatewayUnavailable(AvaAgentError):  # noqa: N818
    """A cross-machine work-row dispatch could not complete — the target
    machine's runner did not pick up the row within the timeout, or the
    target machine reported an unrecognized failure.

    Pairs with `GatewayUnavailable` (SDK -> gateway HTTP unreachable);
    this one is wire-encoded because the gateway is still
    alive and can return a normal response to the SDK. The SDK reconstructs
    this exception for the caller, clearly distinguishing from "the
    gateway itself is unreachable" (one cluster node down vs the entire
    gateway down).
    """

    reason = ErrorReason.CROSS_MACHINE_GATEWAY_UNAVAILABLE
    http_status = 502


class InvalidModelConfig(AvaAgentError):  # noqa: N818 — state description, same style as AgentNotFound
    """The model config (llm_model) in the spawn request or cluster defaults
    is invalid — model name unknown or its provider's API key is not configured.

    Set the required key in ~/.ava/.env or override llm_model in the spawn
    config to a model whose provider key is available.
    """

    reason = ErrorReason.INVALID_MODEL_CONFIG
    http_status = 400


# Completeness check: every ErrorReason must have a registered class
# (after all AvaAgentError subclasses have run __init_subclass__ on
# import, the two sets must be exactly equal). Otherwise "enum added
# but class missing" — wire protocol has a hole; fail at import time.
# Use raise (not assert) because ruff S101 + python -O strips asserts;
# this contract check must run in prod too.
if set(EXCEPTION_BY_REASON) != set(ErrorReason):
    raise RuntimeError(
        f"ErrorReason and EXCEPTION_BY_REASON are inconsistent — "
        f"reasons without a class: {set(ErrorReason) - set(EXCEPTION_BY_REASON)};"
        f"classes without a reason: {set(EXCEPTION_BY_REASON) - set(ErrorReason)}"
    )

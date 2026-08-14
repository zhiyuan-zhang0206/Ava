"""Shared update trigger + process-level cooldown for the schema and pin controllers.

Both the schema-reconcile and pin-drift controllers converge by triggering a
local ``ava cluster update`` — a ``cluster_update`` op POSTed to THIS host's own ops
server over loopback, never through the gateway. The ops server is the
host-local executor of cluster ops (it is what a rollout's Phase A/B fan-out
dials anyway); a self-heal that went through the gateway would add a hop and
make the heal depend on exactly the component an update takes down. The two
controllers share ONE process-level cooldown so two dimensions drifting in the
same tick cannot fire two updates — the *attempt* is the rate-limited event.
This lives in its own module so both controllers import the same cooldown
state (it was a single module-global in the watchdog); splitting them would
create two independent cooldowns that no longer guard each other.
"""

from __future__ import annotations

import logging
import time

import httpx

from ops.rpc_schemas import OpEnvelope, OpResponse
from shared.cluster_auth import bearer_header
from shared.config import settings
from shared.daemon_health import health_port
from shared.http_dial import post as dial_post

_log = logging.getLogger("ops.controllers.update_trigger")

# Minimum interval between update triggers within one process. Prevents a tick
# from re-triggering before the previous update finishes (the update mid-flow runs
# `ava stop`, which kills this process; the next process is fresh and the cooldown
# naturally resets — so this only covers the "same process, consecutive ticks"
# window).
UPDATE_COOLDOWN_S = 120.0
_last_update_spawn: float = 0.0

# Consecutive failure counter. After _ESCALATE_AFTER_N failures (~10 min at
# 120s cooldown), escalate from WARNING to ERROR so the failure becomes visible
# in agent_events (shared/log.py routes ERROR there). Reset on any success.
_consecutive_failures: int = 0
_ESCALATE_AFTER_N = 5


def in_cooldown() -> bool:
    """True if an update was triggered within ``UPDATE_COOLDOWN_S`` of now."""
    return (time.monotonic() - _last_update_spawn) < UPDATE_COOLDOWN_S


def _local_ops_url() -> str:
    """This host's own ops server URL — loopback on the unit's ops port.

    Deliberately no `machines`-table read: a self-heal may fire precisely when
    Postgres is unreachable, and the ops port is local config
    (`health_port('ops')`), never something to look up in a database.
    """
    return f"http://127.0.0.1:{health_port('ops')}/ops"


def trigger_update(target_sha: str | None = None) -> bool:
    """Trigger ``ava cluster update`` via this host's own ops server. Returns True iff the
    ops server accepted it (so a caller never records a heal that did not fire).

    Named `trigger_update` (not `spawn_update`) on purpose: this function only
    POSTs a `cluster_update` op to the local ops server — the actual updater
    session is spawned by `ops.cluster.spawn_update` on the other side of that
    hop, and the two must not share a name (audit #01-update-sequence #3).

    Single contract: ``POST /ops`` with a ``cluster_update`` op on loopback —
    the same authenticated surface the gateway's rollout fan-out dials, minus
    the gateway hop. ``target_sha`` pins the force-checkout commit (the off-pin
    self-heal converges to exactly the cluster pin, not the moving origin/main
    tip); absent, the update catches up to origin/main (or the track mode's
    target).
    """
    global _last_update_spawn, _consecutive_failures  # noqa: PLW0603
    _last_update_spawn = time.monotonic()  # arm cooldown before the attempt — the
    # *attempt* is the rate-limited event
    url = _local_ops_url()
    payload = OpEnvelope(
        kind="cluster_update",
        payload=(
            {"target_sha": target_sha, "mode": "smooth"}
            if target_sha is not None
            else {"mode": "smooth"}
        ),
    )
    secret = settings.data_plane.cluster_secret
    headers = bearer_header(secret) if secret else {}
    try:
        # exclude_none: this POST is one-shot (no retry loop), so it carries no
        # idempotency key — and its wire stays byte-identical to pre-#961.
        resp = dial_post(
            url, timeout=10.0, json=payload.model_dump(exclude_none=True), headers=headers
        )
        resp.raise_for_status()
        envelope = OpResponse.model_validate(resp.json())
        if envelope.status != "completed":
            _log.warning("[ops] local ops server rejected cluster_update: %r", envelope.result)
            return False
        _log.info("[ops] update triggered via local ops server: %r", envelope.result)
        _consecutive_failures = 0  # reset on success
        return True
    except httpx.HTTPError as exc:
        _consecutive_failures += 1
        if _consecutive_failures >= _ESCALATE_AFTER_N:
            _log.error(
                "[ops] failed to trigger update %d consecutive times (last: %r) — "
                "self-heal is failing silently; check the local ops server at %s",
                _consecutive_failures,
                exc,
                url,
            )
        else:
            _log.warning("[ops] failed to trigger update: %r", exc)
        return False
    except Exception as exc:  # a malformed /ops body must not crash the watchdog tick
        _consecutive_failures += 1
        _log.warning("[ops] failed to trigger update (unexpected %r): %r", type(exc).__name__, exc)
        return False


def arm_cooldown() -> None:
    """Arm the shared cooldown without going through the ops-server trigger.

    For a controller that converges by spawning the updater locally (the code
    controller's stale-process restart) instead of POSTing a ``cluster_update``
    op. The cooldown must still be armed — its job is to stop two dimensions
    drifting in the same tick from firing two updates, and which transport a
    heal used does not change that.
    """
    global _last_update_spawn  # noqa: PLW0603
    _last_update_spawn = time.monotonic()


def reset_cooldown() -> None:
    """Clear the process cooldown. Test seam — real code only ever arms it."""
    global _last_update_spawn  # noqa: PLW0603
    _last_update_spawn = 0.0

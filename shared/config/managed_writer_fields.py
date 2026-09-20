"""The managed-writer activation-window field block of `GatewaySettings` (task #4129).

Split out of `shared/config/gateway.py` at the file-size budget (the same split
pattern as `delivery_watchdog_fields.py`, task #2624, and
`update_spawn_fields.py`, task #4117). Mixed into `GatewaySettings` -- NOT a
config domain: `settings.gateway.update_managed_writer_window_seconds`, its
alias/scope face and the `.env` contract stay on the gateway domain. Read by the
managed-writer begin position when it takes the window V. Task #4129 I5 adds the
collection phase's two cadence knobs (the hop wait's ledger poll and the per-unit
read timeout) to the same family.
"""

from __future__ import annotations

from pydantic import Field


class ManagedWriterFields:
    """The managed-writer dispatch fields, in `gateway` domain order."""

    update_managed_writer_window_seconds: float = Field(
        default=7200.0,
        gt=0,
        allow_inf_nan=False,
        alias="AVA_UPDATE_MANAGED_WRITER_WINDOW_SECONDS",
        description=(
            "Ceiling (seconds) on the managed-writer activation window V = min(the "
            "live deploy lease's remaining time, this). V is taken once per begin "
            "execution, carried by the sealed prepared plan and projected into every "
            "later phase, so a lease renewal during the rollout never slides it (a "
            "re-run begin recomputes V against the then-current lease; registering V "
            "durably with the pending journal is the collection phase's, task #4129 "
            "I4); past V the sealed pre-stop evidence (prepared receipts and the "
            "operator plan, produced before the stop) is stale and the activation "
            "must be re-prepared rather than reused -- that staleness bound is why a "
            "ceiling exists at all. 2h = 4x the 30m deploy-lease TTL (LOCK_TTL_S): a "
            "healthy rollout renews its lease throughout and completes well inside "
            "one lease lifetime (Phase A <=5m10s, the local leg minutes, Phase B op "
            "polls at 2m granularity, the 15m settle window for stragglers), so the "
            "cap never binds a normal rollout; it stops a pathological renewal chain "
            "from keeping hours-old pre-stop evidence actionable. Must be finite and "
            "positive."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    update_managed_writer_hop_poll_seconds: float = Field(
        default=10.0,
        gt=0,
        allow_inf_nan=False,
        alias="AVA_UPDATE_MANAGED_WRITER_HOP_POLL_SECONDS",
        description=(
            "Cadence (seconds) of the managed-writer hop wait's per-unit ledger poll "
            "(task #4129 I5). The wait runs between the hop gate and the collection: "
            "each pass reads every not-yet-ready unit's bounded hop ledger over one "
            "small HTTP round trip and prints a line only when a unit's stage changes "
            "(the hop advances through minutes-scale stages: quiesce, old stop, "
            "candidate boot, readiness). The poll is a liveness probe with almost no "
            "load, and a minute-scale stage transition must be visible within a few "
            "polls rather than only at the window's deadline check -- seconds, not "
            "minutes. Must be finite and positive."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    update_managed_writer_pull_timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        allow_inf_nan=False,
        alias="AVA_UPDATE_MANAGED_WRITER_PULL_TIMEOUT_SECONDS",
        description=(
            "Per-request timeout (seconds) of the collection phase's unit reads -- the "
            "observer read and the hop ledger read (task #4129 I5). Each read is one "
            "bounded JSON response from a unit's restricted observer over the cluster "
            "network, the same order of magnitude as the ops plane's other unit calls; "
            "15s leaves room for a slow-but-live unit while keeping a dead host's "
            "failure inside the evaluate-the-roster pass instead of stalling the "
            "rollout's collection window. Must be finite and positive."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

"""The managed-writer activation-window field block of `GatewaySettings` (task #4129).

Split out of `shared/config/gateway.py` at the file-size budget (the same split
pattern as `delivery_watchdog_fields.py`, task #2624, and
`update_spawn_fields.py`, task #4117). Mixed into `GatewaySettings` -- NOT a
config domain: `settings.gateway.update_managed_writer_window_seconds`, its
alias/scope face and the `.env` contract stay on the gateway domain. Read by the
managed-writer begin position when it takes the window V.
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

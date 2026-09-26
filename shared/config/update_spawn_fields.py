"""The updater gated-spawn field block of `GatewaySettings` (task #4117).

Moved out of `shared/config/gateway.py` when the checked normal-release spawn
fields pushed that module past its 800-line hard ceiling (the same split
pattern as `delivery_watchdog_fields.py`, #2624). Mixed into `GatewaySettings`
— NOT a config domain: `settings.gateway.update_spawn_*`, every alias/scope/
capability face, and the `.env` contract stay on the gateway domain. Read by
`shared.spawn_receipt` while adjudicating one gated spawn attempt.
"""

from __future__ import annotations

from pydantic import Field


class UpdateSpawnFields:
    """The gated-spawn adjudication fields, in `gateway` domain order."""

    update_spawn_gate_poll_seconds: float = Field(
        default=0.05,
        gt=0,
        allow_inf_nan=False,
        alias="AVA_UPDATE_SPAWN_GATE_POLL_SECONDS",
        description=(
            "Poll cadence (seconds) of the gated-spawn adjudication loop while the "
            "session gate is held without a birth receipt yet. 0.05 is the same "
            "cadence the POSIX supervisor's kill/exit polling uses "
            "(_KILL_POLL_S) — a poll cadence, not a clock with neighbours."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    update_spawn_receipt_max_bytes: int = Field(
        default=64 * 1024,
        gt=0,
        alias="AVA_UPDATE_SPAWN_RECEIPT_MAX_BYTES",
        description=(
            "Read budget (bytes) for one spawn receipt file. A receipt is a few "
            "hundred bytes of identity fields; anything larger is treated as "
            "unreadable evidence and refused (fail closed) rather than parsed. "
            "The 64 KiB value is the same bounded-read family the session-record "
            "readers use."
        ),
        json_schema_extra={
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

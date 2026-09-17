"""The delivery-outbox field block of `DaemonSettings` (task #3757).

The deferred-delivery outbox gives a message that exhausted its sender-side
retry budget a durable machine-local record and a resident retry loop, so a
gateway outage window cannot silently swallow it (see
`shared/delivery_outbox.py` for the mechanism). These fields live in their own
mixin module for the same line-budget reason as the delivery-watchdog block:
`shared/config/daemon.py` sits at the 800-line ceiling. NOT a config domain —
`settings.daemon.delivery_outbox_*`, every alias/scope/capability face, and the
`.env` contract are exactly as if they were declared in `daemon.py`. The
`delivery_outbox_retry_backoff_steps_s` validators stay on the model there (a
plain mixin's `field_validator` is not collected by pydantic).

Every field is read through the live config path (`current_field_values()` —
`.env` file primary), so an edit takes effect on the next send / flush tick
without a process restart; the metadata says so with `restart_required: ""`.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field
from pydantic_settings import NoDecode


class DeliveryOutboxFields:
    """The deferred-delivery outbox fields of `DaemonSettings`."""

    delivery_outbox_enabled: bool = Field(
        default=True,
        alias="AVA_DELIVERY_OUTBOX_ENABLED",
        description="Deferred-delivery outbox (task #3757): when on, a chat send that exhausts its retry budget is recorded durably on the sending machine and a resident service keeps retrying it; when off, sends behave exactly as before (no record, no redelivery) and existing records stay on disk untouched. Read live by the recorder and the flusher, so a flip applies on the next send / flush tick.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    delivery_outbox_retry_backoff_steps_s: Annotated[list[float], NoDecode] = Field(
        default=[30.0, 60.0, 300.0, 900.0],
        alias="AVA_DELIVERY_OUTBOX_RETRY_BACKOFF_STEPS_S",
        description="Minimum seconds between successive flush attempts for one deferred-delivery entry, indexed by the entry's current flush-attempt count (the first attempt waits steps[0]); the last step repeats. 30s/60s recover restart-sized blips promptly; 5m/15m ride update windows at negligible load; the 15m cap bounds worst-case post-recovery backfill latency while a still-down data plane sees ~96 attempts/day per entry.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_outbox_budget_seconds: float = Field(
        default=43200.0,
        gt=0,
        alias="AVA_DELIVERY_OUTBOX_BUDGET_SECONDS",
        description="Deferred-delivery budget (seconds): how long an outbox entry keeps being redelivered before it is abandoned with an observable escalation instead. 12h is 6.5x the 2026-09-17 110-minute black window (task #3719/#3757 evidence), so an overnight outage still backfills, while a wake for a fire older than half a day has lost its actionability window and escalates rather than delivering stale work.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_outbox_dedup_window_seconds: float = Field(
        default=900.0,
        gt=0,
        alias="AVA_DELIVERY_OUTBOX_DEDUP_WINDOW_SECONDS",
        description="Same-message merge window (seconds): failed sends to the same (machine, target, source, content) within this window of the entry's last recorded attempt merge into one entry sharing one idempotency key, so a sender retry chain journals one logical message, not one per attempt. 15 min covers the reference watchers' 10.5-minute retry chain (10s->160s backoff, task #3694) with margin; identical messages sent further apart stay separate entries.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_outbox_flush_interval_seconds: float = Field(
        default=30.0,
        gt=0,
        alias="AVA_DELIVERY_OUTBOX_FLUSH_INTERVAL_SECONDS",
        description="Deferred-delivery flusher tick (seconds): how often the machine's ops daemon checks the outbox for due entries. Bounds wake-to-backfill latency for short outages at one directory scan per tick (an absent outbox directory is a single stat, the steady-state cost).",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_outbox_max_entries: int = Field(
        default=128,
        ge=1,
        alias="AVA_DELIVERY_OUTBOX_MAX_ENTRIES",
        description="Cap on pending deferred-delivery entries per machine. Bounds worst-case outbox disk (~128 MiB at the 1 MiB transport content limit) and per-tick flush work when a runaway sender keeps producing distinct failed messages; when full, a new failure is not recorded and the send fails loudly with today's semantics. Abandoned entries retained for inspection are not counted.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

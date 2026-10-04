"""The delivery-watchdog and inbound-reconcile field blocks of `DaemonSettings`.

Moved out of `base/config/daemon.py` when two in-flight field additions and
this block together pushed that module past its 800-line hard ceiling (task
#3616 x #3621 line-budget split, 2026-09-16). Mixed into `DaemonSettings` — NOT
a config domain: `settings.daemon.delivery_watchdog_*`, every alias/scope/
capability face, and the `.env` contract stay exactly as they were. The two
`delivery_watchdog_dispatch_backoff_steps_s` validators stay on the model in
`daemon.py`.

The inbound-reconcile window bounds joined the module 2026-09-27 (task #4788)
under the same ceiling. Both blocks bound the same `'claimed'` chat-row
lifecycle: the watchdog sweeps stale claims, the reconcile finalizes them from
the claim window at boot/settle, and the reconcile's fresh-claim scope reads
`delivery_watchdog_stale_claimed_threshold_seconds` from the block above.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field
from pydantic_settings import NoDecode


class DeliveryWatchdogFields:
    """The delivery-watchdog fields, in their former `daemon.py` order."""

    delivery_watchdog_max_resurrect_per_tick: int = Field(
        default=3,
        alias="AVA_DELIVERY_WATCHDOG_MAX_RESURRECT_PER_TICK",
        description="Ceiling on how many terminated-owner resurrect retries the delivery watchdog starts per round (Task #689 G4). A pile of dead letters drains over rounds; the cap plus the 60s per-agent cooldown and 2-way concurrency semaphore prevent an LLM wake storm when many terminated agents hold pending chats.",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_resurrect_fail_before_suppress: int = Field(
        default=5,
        ge=1,
        alias="AVA_DELIVERY_WATCHDOG_RESURRECT_FAIL_BEFORE_SUPPRESS",
        description="Consecutive failed terminated-owner auto-resurrect attempts before the delivery watchdog suppresses that agent's automatic wakes.",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_suppress_base_seconds: float = Field(
        default=1800.0,
        gt=0,
        alias="AVA_DELIVERY_WATCHDOG_SUPPRESS_BASE_SECONDS",
        description="Initial automatic-wake suppression window after repeated terminated-owner resurrection failures; each later suppression for that agent doubles this duration.",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_suppress_max_seconds: float = Field(
        default=86400.0,
        gt=0,
        alias="AVA_DELIVERY_WATCHDOG_SUPPRESS_MAX_SECONDS",
        description="Maximum automatic-wake suppression window after repeated terminated-owner resurrection failures.",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_enabled: bool = Field(
        default=True,
        alias="AVA_DELIVERY_WATCHDOG_ENABLED",
        description="Run the delivery watchdog daemon on the gateway. On by default; set false to disable stale-pending-inbound alerting cluster-wide.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    delivery_watchdog_interval_seconds: float = Field(
        default=0.5,
        alias="AVA_DELIVERY_WATCHDOG_INTERVAL_SECONDS",
        description="Delivery watchdog tick (seconds): how often it re-dispatches lost wakes and scans for stalled inbounds. Constant load (~2 qps), independent of fleet size. Lower = faster wake recovery after a lost publish.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_dispatch_threshold_seconds: float = Field(
        default=1.0,
        alias="AVA_DELIVERY_WATCHDOG_DISPATCH_THRESHOLD_SECONDS",
        description="Wake re-dispatch threshold (seconds): a pending inbound of an idling owner older than this gets its Redis wake re-published (plus the wake-key breadcrumb). Must stay below the claim loop's 30s SELECT recheck — it is the fast fallback for lost pub/sub wakes.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_max_dispatch_count: int = Field(
        default=5,
        ge=1,
        alias="AVA_DELIVERY_WATCHDOG_MAX_DISPATCH_COUNT",
        description="Maximum successful delivery-watchdog wake re-dispatches for one pending inbound before the watchdog poisons the row and stops re-publishing. Poison does not prevent the agent from claiming the inbound after recovery.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_dispatch_backoff_steps_s: Annotated[list[float], NoDecode] = Field(
        default=[5.0, 30.0, 120.0, 300.0],
        alias="AVA_DELIVERY_WATCHDOG_DISPATCH_BACKOFF_STEPS_S",
        description="Minimum seconds between successive delivery-watchdog wake re-dispatches, indexed by the row's current dispatch count (the first re-dispatch waits steps[0]). The last step repeats when the dispatch cap is longer than this list.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_host_staleness_seconds: float = Field(
        default=120.0,
        gt=0,
        alias="AVA_DELIVERY_WATCHDOG_HOST_STALENESS_SECONDS",
        description="Host-verdict freshness window (seconds) for wake re-dispatch and poisoning: a row is dispatched or poisoned only while its owner's machine_probe row is fresh, the machine not graded offline (fewer than two consecutive probe failures), and the agent host alive — the host check is excused inside the one-failure grace window, because a failed probe necessarily nulls the host verdict. A stale, missing, offline-graded or (outside that window) host-less verdict freezes the row — no re-dispatch, no dispatch-count advance, no poison — and redelivery resumes on the next round once the verdict is fresh again (task #4872 route D: an outage must not burn a row's dispatch budget). The default covers the 60s liveness-pass cadence with its two-consecutive-failure grading, the same freshness contract the roster's availability read uses.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_threshold_seconds: float = Field(
        default=30.0,
        alias="AVA_DELIVERY_WATCHDOG_THRESHOLD_SECONDS",
        description="Delivery watchdog stall alert threshold (seconds): a chat inbound still pending longer than this whose owner is in a waiting/terminal state (idling/terminated) is reported as a stalled delivery.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_alert_grace_seconds: float = Field(
        default=300.0,
        ge=0.0,
        alias="AVA_DELIVERY_WATCHDOG_ALERT_GRACE_SECONDS",
        description="Settle window (seconds) between a deploy window closing (or a daemon boot / stop-window resume) and stall alerting resuming: the resume's claim tail is still being claimed, so its pending rows are not stalls yet — the 2026-10-03/04 post-ready batches all claimed within +228s of ready. Wake re-dispatch and the dead-letter sweeps keep running throughout; 0 disables the grace.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_stalled_recovery_enabled: bool = Field(
        default=True,
        alias="AVA_DELIVERY_STALLED_RECOVERY_ENABLED",
        description="Stalled crash-marked recovery (task #3618): when on, the delivery watchdog asks the owner's home runner for a harvest decision (`recover-crash-marked-v2`) once a chat inbound has been pending past the stall threshold while its owner is a crash-marked idling corpse. Off leaves alerting only — the stalled backlog is reported but never harvested automatically.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_stale_claimed_threshold_seconds: float = Field(
        default=86400.0,
        alias="AVA_DELIVERY_WATCHDOG_STALE_CLAIMED_THRESHOLD_SECONDS",
        description="Stale-claimed dead-letter threshold (seconds): a 'claimed' chat inbound whose owner is terminated and whose claim is older than this (claimed_at, falling back to created_at for pre-column rows) is flipped to 'done' instead of being re-delivered if the agent is ever resurrected. Terminated agents leave claimed rows behind (reconcile runs only at boot); a resurrect would otherwise flip them all to 'pending' and re-deliver ancient messages (Task #654).",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    delivery_watchdog_stale_claimed_idling_threshold_seconds: float = Field(
        default=7200.0,
        alias="AVA_DELIVERY_WATCHDOG_STALE_CLAIMED_IDLING_THRESHOLD_SECONDS",
        description="Stale-claimed dead-letter threshold for IDLING owners (seconds): a 'claimed' chat inbound whose owner is idling and whose claim is older than this is flipped to 'done'. Hosted agents stay idling without booting, so their claims never hit the reconcile path; running owners are never swept.",
        json_schema_extra={
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )


class InboundReconcileFields:
    """The inbound-reconcile window bounds, in their former `daemon.py` order."""

    inbound_reconcile_clock_pad_seconds: float = Field(
        default=300.0,
        gt=0,
        alias="AVA_INBOUND_RECONCILE_CLOCK_PAD_SECONDS",
        description="Hosted agent-runner: seconds subtracted from the oldest fresh claim creation when bounding the inbound reconcile's fast write window. A larger pad may fetch more rows. Any unproven fresh claim triggers a timestamp-independent settled-write scan, then a checkpoint read if needed, so this bound affects cost, not correctness.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    inbound_reconcile_boundary_scan_limit: int = Field(
        default=1024,
        ge=1,
        alias="AVA_INBOUND_RECONCILE_BOUNDARY_SCAN_LIMIT",
        description="Hosted agent-runner: maximum newest checkpoints scanned to find the first one older than the earliest fresh claim. An unresolved boundary triggers a timestamp-independent settled-write scan, then a checkpoint read if needed. This limit bounds the fast path's cost, not correctness.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

    inbound_reconcile_window_row_cap: int = Field(
        default=256,
        ge=1,
        alias="AVA_INBOUND_RECONCILE_WINDOW_ROW_CAP",
        description="Hosted agent-runner: maximum messages-channel write rows fetched in the fast claim window (about 2.5 MB at the observed 10 KB per row). A larger window triggers a streaming settled-ancestor scan, then a checkpoint read for any fresh id still unproven. This cap bounds the fast path's memory and query cost.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )

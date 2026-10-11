"""Delivery observation and stale-input retirement settings."""

from __future__ import annotations

from pydantic import Field


class DeliveryWatchdogFields:
    """The delivery-watchdog fields, in their former `daemon.py` order."""

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
        default=30.0,
        alias="AVA_DELIVERY_WATCHDOG_INTERVAL_SECONDS",
        description="Interval in seconds for observing stalled inbounds and retiring stale rows. Pending input pickup is owned by the agent host.",
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

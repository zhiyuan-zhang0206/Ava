"""The billing batch-recovery field block of `DaemonSettings` (task #3919).

Mixed into `DaemonSettings` — NOT a config domain: `settings.daemon.billing_recovery_*`,
every alias/scope/capability face, and the `.env` contract stay on the one daemon
domain (no new domain surface; mirrors the delivery-watchdog block split).
Operated through the explicit `ava agents resurrect-billing` entry.
"""

from __future__ import annotations

from pydantic import Field


class BillingRecoveryFields:
    """The billing batch-recovery fields, in `daemon` domain order."""

    billing_recovery_min_balance: float = Field(
        default=1.0,
        gt=0,
        alias="AVA_BILLING_RECOVERY_MIN_BALANCE",
        description="Minimum provider account balance (the largest balance_infos total, in the account's currency) the billing batch-recovery entry accepts before resurrecting the billing-class halt victims. A floor just above zero is deliberate: it cannot be misread as zero/exhausted, and it is orders of magnitude below any real top-up, so the gate rejects only a not-yet-funded account; raise it when resuming should require headroom for more work than the first turns.",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    billing_recovery_balance_url: str = Field(
        default="https://api.deepseek.com/user/balance",
        alias="AVA_BILLING_RECOVERY_BALANCE_URL",
        description="Provider balance endpoint the billing batch-recovery entry probes (GET with the DEEPSEEK_API_KEY bearer). DeepSeek's documented /user/balance returns is_available plus per-currency balance_infos; point this at another provider's equivalent only after confirming the same payload shape — a parse failure is fail-closed (the run refuses).",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    billing_recovery_balance_timeout_s: float = Field(
        default=10.0,
        gt=0,
        alias="AVA_BILLING_RECOVERY_BALANCE_TIMEOUT_S",
        description="Per-attempt timeout for the balance probe. 10s bounds the operator-facing check on a slow link while staying far above the endpoint's sub-second normal latency; a timeout is fail-closed (the run refuses rather than acting on an unverified account).",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    billing_recovery_dispatch_concurrency: int = Field(
        default=6,
        ge=1,
        alias="AVA_BILLING_RECOVERY_DISPATCH_CONCURRENCY",
        description="How many billing-recovery resurrect dispatches run at once across all home machines. Bounds the launch fan-out so a fleet-scale recovery (23 agents in the 2026-09-18 incident) does not stampede home agent-hosts: 6 drains that cohort in a few waves while keeping each host's simultaneous launch burst small.",
        json_schema_extra={
            "capability": "gateway",
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

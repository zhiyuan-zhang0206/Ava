"""Operator inspection for local PITR retention dry-run plans."""

from __future__ import annotations

import json

from services.pitr.retention_planner import inspect_dry_run_plan
from shared.paths import ava_home


def cmd_pitr_retention_inspect() -> int:
    plan = inspect_dry_run_plan(ava_home() / "physical-backup")
    print(
        json.dumps(
            {
                "plan_digest": plan.digest(),
                "blocked_reasons": plan.blocked_reasons,
                "protected_chain_ids": plan.protected_chain_ids,
                "unprotected_chain_ids": plan.unprotected_chain_ids,
                "oldest_retained_chain_id": plan.oldest_retained_chain_id,
                "ack_high_water": plan.ack_high_water,
                "retained_objects": len(plan.retained),
                "eligible_objects": len(plan.eligible),
                "retained_bytes": plan.retained_bytes,
                "eligible_bytes": plan.eligible_bytes,
                "delete_enabled": False,
            },
            sort_keys=True,
        )
    )
    return 0 if not plan.blocked_reasons else 2

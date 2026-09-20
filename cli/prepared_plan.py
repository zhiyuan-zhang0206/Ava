"""Validate one dispatched sealed plan inside the candidate image, then acknowledge it.

Channel B of task #4129 (design:
`managed-writer-dispatch-design-20260920.md` section 3.2): this entry runs
INSIDE the verified candidate image -- its code must be loaded from the image
the plan names -- and answers the coordinator's `cluster_prepare_dispatch` with
the digests of exactly what it validated:

- the sealed plan's own bytes: the file name and the reported digest must agree
  with the bytes read, and every cross-reference (coordinator, complete roster,
  recovery images, schema baseline, deadline) is checked by
  `prepare_operator_input`;
- the local unit's prepared-receipt digest, re-derived by
  `prepare_operator_input` from the receipt file the plan binds, so the
  coordinator can hold this acknowledgement against the facts it gathered
  before the fleet stops.

Read-only: no stop, no migration, no selector write, no service start, no
converge. One JSON acknowledgement goes to stdout on success; any refusal is
exit 2 with a sanitized stderr line (never a credential). The caller -- the
unit's ops daemon answering `cluster_prepare_dispatch`
(`ops.ops_prepare_dispatch`) -- relays the acknowledgement; the coordinator
re-derives every binding from the wire values it receives
(`cli.commands._managed_writer_dispatch`), so this entry's output is evidence,
never authority.

Invoked by the ops handler as:

    <home>/releases/<artifact_digest>/venv/bin/python -I -B -X utf8 -m cli.prepared_plan --prepared <path>

The plan path lives under the unit's private `run/`; the child environment
carries no database projection -- the validation is local and read-only and
never dials the cluster.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cli.prepared_update import prepare_operator_input
from ops.rpc_prepare_dispatch import DispatchValidation, prepared_plan_name
from shared.machine import MachineRoleMissing
from shared.managed_writer_barrier import ManagedWriterBarrierError
from shared.native_job_observation import NativeReadUnavailableError
from shared.runtime_release import ReleaseRejectedError


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", required=True)
    return parser


def produce_ack(path: Path) -> dict[str, object]:
    """The ordered read-only validation; every failure is a refusal, never a partial ack."""
    validated = prepare_operator_input(path)
    if path.name != prepared_plan_name(validated.digest):
        raise ReleaseRejectedError("prepared plan is not named for its own bytes")
    ack = DispatchValidation(
        plan_digest=validated.digest,
        receipt_digest=validated.local.unit.prepared_receipt_digest,
    )
    return ack.model_dump(mode="json")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        ack = produce_ack(Path(args.prepared))
    except (
        OSError,
        ValueError,
        ReleaseRejectedError,
        ManagedWriterBarrierError,
        NativeReadUnavailableError,
        MachineRoleMissing,
    ) as exc:
        sys.stderr.write(f"prepared plan refused: {exc}\n")
        return 2
    sys.stdout.write(_canonical(ack) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

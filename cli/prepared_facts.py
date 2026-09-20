"""Produce one unit's prepared-facts shipment for a managed-writer rollout.

Channel A of task #4129 (design: `managed-writer-dispatch-design-20260920.md`
§3.1): this entry runs INSIDE the verified candidate image — its code must be
loaded from the image it reports — and produces the unit's expected-inventory
facts before any stop effect:

- seals the prepared inventory receipt (`_release_inventory.prepare_unit_inventory`);
- derives the candidate normal-start plan (`prepare_normal_services`);
- reads the current selector for the later hop projection's predecessor binding.

Read-only: no stop, no migration, no selector write, no service start, no
converge. One JSON shipment goes to stdout on success; any refusal is exit 2
with a sanitized stderr line (never a credential). The caller — the unit's ops
daemon answering `cluster_prepare_facts` (`ops.ops_prepare_facts`) — relays the
shipment; the coordinator re-verifies every digest from the bytes it receives
(`cli.commands._managed_writer_gather`), so this entry's output is evidence,
never authority.

Invoked by the ops handler as:

    <home>/releases/<artifact_digest>/venv/bin/python -I -B -m cli.prepared_facts ...

`AVA_DB_URL` arrives in an explicit child projection (the ops handler builds
it); argv never carries credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
from datetime import datetime
from pathlib import Path

import psycopg

from cli.commands._release_inventory import prepare_unit_inventory
from cli.commands._release_selector import read_selector, selector_bytes
from cli.commands._release_services import prepare_normal_services
from ops.rpc_prepare_facts import ImageRef
from shared.managed_writer_barrier import RolloutIdentity, lock_rollout
from shared.managed_writer_publication import CandidateUnitPlan, PublishedUnit
from shared.runtime_interpreter import WHEEL_RUNTIME, runtime_venv
from shared.runtime_publication_input import _receipt_expected
from shared.runtime_release import (
    ReleaseRejectedError,
    VerifiedRelease,
    file_sha256,
    verify_release,
)
from shared.verified_file import regular_bytes


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _acquired_at(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("operation acquired-at must carry a timezone offset")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation-holder", required=True)
    parser.add_argument("--operation-acquired-at", required=True, type=_acquired_at)
    parser.add_argument("--operation-target-sha", required=True)
    parser.add_argument("--artifact-digest", required=True)
    parser.add_argument("--manifest-digest", required=True)
    parser.add_argument("--schema-digest", required=True)
    parser.add_argument("--recovery-artifact-digest", required=True)
    parser.add_argument("--recovery-manifest-digest", required=True)
    parser.add_argument("--recovery-schema-digest", required=True)
    return parser


def _loaded_unit() -> tuple[Path, Path]:
    """The canonical (home, release root) this entry is loaded from."""
    if not WHEEL_RUNTIME:
        raise ReleaseRejectedError("prepared facts require the verified candidate runtime")
    prefix = runtime_venv()
    root = prefix.parent
    home = root.parent.parent
    if (
        prefix.name != "venv"
        or root.parent.name != "releases"
        or home.resolve(strict=True) != home
        or not Path(__file__).resolve().is_relative_to(prefix)
    ):
        raise ReleaseRejectedError("prepared facts are not loaded from a canonical unit release")
    return home, root


def _verify_release(ref: ImageRef, store: Path) -> VerifiedRelease:
    return verify_release(
        store,
        ref.artifact_digest,
        manifest_digest=ref.manifest_digest,
        platform_tag=platform.platform(),
        schema_digest=ref.schema_digest,
    )


def produce_facts(args: argparse.Namespace) -> dict[str, object]:
    """The ordered read-only production; every failure is a refusal, never a partial write."""
    if sys.platform != "linux":
        # Q7 fail-closed before begin: the restricted hop has a native proof on
        # Linux/crontab only (as `prepare_bootstrap_hop` restates); a unit on
        # another platform must refuse at preparation, never inside the hop.
        raise ReleaseRejectedError("prepared facts require a platform the restricted hop supports")
    home, root = _loaded_unit()
    if root.name != args.artifact_digest:
        raise ReleaseRejectedError(
            "prepared facts are not loaded from the announced candidate image"
        )
    candidate_ref = ImageRef(
        artifact_digest=args.artifact_digest,
        manifest_digest=args.manifest_digest,
        schema_digest=args.schema_digest,
    )
    recovery_ref = ImageRef(
        artifact_digest=args.recovery_artifact_digest,
        manifest_digest=args.recovery_manifest_digest,
        schema_digest=args.recovery_schema_digest,
    )
    store = root.parent
    image = _verify_release(candidate_ref, store)
    if image.root != root:
        raise ReleaseRejectedError("verified candidate image differs from the loaded runtime")
    package_root = Path(__file__).resolve().parent.parent
    if file_sha256(package_root / "db/schema.sql") != args.schema_digest:
        raise ReleaseRejectedError("candidate schema baseline differs from the announced digest")
    recovery = _verify_release(recovery_ref, store)
    if recovery.digest == image.digest:
        raise ReleaseRejectedError("recovery image must be distinct from the candidate")
    machine = (home / "machine_name").read_text(encoding="utf-8").strip()
    if not machine:
        raise ReleaseRejectedError("installed unit machine identity is empty")
    operation = RolloutIdentity(
        holder=args.operation_holder,
        acquired_at=args.operation_acquired_at,
        target_sha=args.operation_target_sha,
    )
    url = os.environ.get("AVA_DB_URL")
    if not url:
        raise ReleaseRejectedError("prepared facts require an explicit database projection")
    with psycopg.connect(url, connect_timeout=5) as conn, conn.transaction():
        lock_rollout(conn, operation)
        row = conn.execute(
            "SELECT home FROM machine_units WHERE machine_name=%s AND home=%s",
            (machine, str(home)),
        ).fetchone()
        if row != (str(home),):
            raise ReleaseRejectedError("prepared facts unit is not registered")
        receipt_path = prepare_unit_inventory(
            conn, image, home, machine, schema_digest=args.schema_digest
        )
    receipt_bytes = regular_bytes(receipt_path)
    if not receipt_bytes.isascii():
        raise ReleaseRejectedError("sealed receipt is not ASCII JSON")
    prepared_receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
    expected = _receipt_expected(receipt_bytes)
    unit = PublishedUnit(
        machine=expected.machine,
        home=expected.home,
        inventory_digest=expected.unit().inventory_digest,
        prepared_receipt_digest=prepared_receipt_digest,
        artifact_digest=image.digest,
        manifest_digest=image.manifest_digest,
    )
    services = tuple(
        sorted(
            (item.identity for item in prepare_normal_services(unit, args.schema_digest)),
            key=lambda item: item.session,
        )
    )
    current = read_selector(home)
    if current is not None and not current.isascii():
        raise ReleaseRejectedError("current selector is not ASCII JSON")
    candidate = CandidateUnitPlan(
        unit=unit,
        services=services,
        previous_selector_digest=(
            hashlib.sha256(current).hexdigest() if current is not None else None
        ),
        selector_digest=hashlib.sha256(selector_bytes(unit)).hexdigest(),
    )
    return {
        "unit": unit.model_dump(mode="json"),
        "receipt_json": receipt_bytes.decode("ascii"),
        "candidate": candidate.model_dump(mode="json"),
        "recovery": recovery_ref.model_dump(mode="json"),
        "previous_selector": current.decode("ascii") if current is not None else None,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        shipment = produce_facts(args)
    except psycopg.Error as exc:
        # Never expose credential-bearing connection diagnostics.
        sys.stderr.write(f"prepared facts refused ({type(exc).__name__})\n")
        return 2
    except (OSError, ValueError, ReleaseRejectedError) as exc:
        sys.stderr.write(f"prepared facts refused: {exc}\n")
        return 2
    sys.stdout.write(_canonical(shipment) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

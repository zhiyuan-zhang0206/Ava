"""The candidate image's `receipt` and `preflight` handoff entries (frozen v1 names).

Both run read-only on the unit, as the image the document names as its
executor (`cli.release_handoff.__main__` enforces that), so the code that
answers is always the candidate's, whatever image the unit currently runs.

- `receipt`: the unit's facts a coordinator needs to include it
  (`request.UnitReceipt`): identity, roles, ABI tag, native adapter, its
  selected release, this candidate (fully verified here), the candidate's
  SQL inventory digest, the configuration digest and the installed
  enrollment id. The document is any v1 envelope that names this unit and
  the candidate as executor.
- `preflight`: the unit's own `UnitRequest` is admissible here now: the
  gates its `submit` applies (configuration, image pair, topology), its
  selection is the request's predecessor, no other home operation is
  incomplete, and the installed enrollment is the one the request names.
  It prints `{"ready": true}`; any refusal exits 2 and changes nothing.

The request builder learns a remote unit's candidate reference only once
`prepare` publishes the unit's receipt to the gateway (slice FC-7b,
decisions/2026-09-27-fleet-core-release-choices.md), and remote units wait
for slice dbgen-8, so no production path calls these entries yet.
"""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

from base.api_contracts.release_handoff import read_envelope
from cli.release_fleet.request import AdapterKind, UnitReceipt, UnitRequest, sql_inventory_digest


def _adapter() -> AdapterKind:
    from base.host.system.boot_unit import systemd_running

    if systemd_running():
        return "linux-systemd-v1"
    if sys.platform == "darwin":
        return "darwin-launchd-v1"
    raise ValueError("this host has no native release executor adapter")


def receipt(encoded: bytes) -> UnitReceipt:
    from base.cluster import registry_path
    from base.cluster.authority.unit import load_unit_enrollment
    from base.cluster.machine import machine_name, machine_role
    from base.deploy.release.start_inputs import configuration_digest
    from base.runtime_abi import current_abi
    from cli.release_operator.current import current_release
    from cli.release_transition.request import ReleaseRef

    envelope = read_envelope(encoded)
    home = Path(envelope.home)
    if envelope.machine != machine_name():
        raise ValueError(f"the document names machine {envelope.machine!r}, not this unit")
    candidate = ReleaseRef.model_validate(envelope.executor.model_dump())
    image = candidate.verify(home)
    selected = current_release(home)
    enrollment = load_unit_enrollment(home)
    return UnitReceipt(
        machine=envelope.machine,
        home=envelope.home,
        registry=str(registry_path()),
        roles=tuple(sorted(machine_role())),
        abi=current_abi().to_json(),
        platform=platform.platform(),
        adapter=_adapter(),
        previous=None if selected is None else selected[0],
        candidate=candidate,
        sql_inventory_digest=sql_inventory_digest(image),
        configuration_digest=configuration_digest(home),
        enrollment_id=None if enrollment is None else enrollment.enrollment_id,
    )


def preflight(encoded: bytes) -> dict[str, object]:
    from base.cluster.authority.unit import load_unit_enrollment
    from base.deploy.release.runtime_release import current_pointer
    from base.deploy.release.verified_file import regular_bytes
    from cli.release_transition.journal import read_operation, read_request
    from cli.release_transition.local import LocalTransition

    request = read_request(encoded)
    if not isinstance(request, UnitRequest):
        raise ValueError("a unit preflight reads a unit request")  # noqa: TRY004 — a refused document, not a type error
    home = Path(request.home)
    LocalTransition(request).preflight()
    if current_pointer(home / "releases") != request.previous.selector:
        raise ValueError("this unit's selected release is not the request's predecessor")
    try:
        active = Path(regular_bytes(home / "updates" / "active").decode().strip())
    except FileNotFoundError:
        active = None
    if active is not None and active != request.path and not read_operation(active).terminal:
        raise ValueError("another release operation on this unit remains incomplete")
    enrollment = load_unit_enrollment(home)
    if enrollment is None or enrollment.enrollment_id != request.enrollment_id:
        raise ValueError("this unit's installed enrollment is not the one the request names")
    return {"ready": True, "unit": request.unit.label, "operation": str(request.id)}


def run(entry: str, encoded: bytes) -> int:
    """Print the entry's one JSON object; a refusal exits 2."""
    try:
        answer = (
            receipt(encoded).model_dump(mode="json") if entry == "receipt" else preflight(encoded)
        )
    except (ValueError, OSError, RuntimeError) as exc:
        sys.stderr.write(f"release {entry} refused: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(answer, sort_keys=True) + "\n")
    return 0

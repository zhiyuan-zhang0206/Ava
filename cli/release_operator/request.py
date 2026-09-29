"""`ava cluster release request` — build the fleet's release request on the gateway home.

The gateway unit's own images: `previous` is this home's selected release,
re-verified (`cli.release_operator.current`); `candidate` is the `prepare`
receipt for `--commit` (at `$AVA_HOME/releases/work/<commit>/receipt.json`, or
an explicit `--receipt`). Every other registered unit (`machine_units`) is
accounted for, read-only:

- a paused machine's units are excluded (reason `paused`);
- `--exclude MACHINE:HOME --reason R` excludes one unit (reason `operator`);
- any other unit would take part, which needs its receipt collected through
  the image-exec handoff and its capability delivered over the coordinator
  channel: networked releases wait for slice dbgen-8, and the refusal names it.

A single box is a fleet of one. `--watch-s` shortens the captured watch
window (the preview's cycle uses it); every other policy bound keeps its
`FleetPolicy` default. `--alert-agent` and `--alert-webhook-file` route the
fleet's alerts to an observing agent and to the out-of-band webhook whose URL
the owner-only `$AVA_HOME/secrets/<file>` holds (checked now, so a missing or
readable secret refuses here, not at the first alert);
`--acknowledged-rejection` names the operation that rejected the candidate,
the only way to request a rejected candidate again. The written file is
handed to `ava cluster update --prepared`; nothing here submits or
dispatches it.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from cli.release_fleet.inventory import NETWORKED_REFUSAL, check_inventory, registered_units
from cli.release_fleet.policy import AlertRoute, FleetPolicy, UnitKey
from cli.release_fleet.request import Exclusion, FleetRequest
from cli.release_operator.current import current_release
from cli.release_operator.layout import receipt_path, require_commit_shape
from cli.release_prepare.models import PreparationReceipt
from cli.release_transition.request import ReleaseRef, verify_pair
from shared.verified_file import regular_bytes


def _candidate_from_receipt(home: Path, commit: str, receipt: Path | None) -> ReleaseRef:
    path = receipt_path(home, commit) if receipt is None else receipt
    try:
        encoded = regular_bytes(path)
    except FileNotFoundError:
        raise ValueError(
            f"no prepared receipt at {path} — run "
            f"`ava cluster release prepare --commit {commit}` on this host first"
        ) from None
    prepared = PreparationReceipt.model_validate_json(encoded)
    if prepared.source.source_commit != commit:
        raise ValueError("prepared receipt names a different commit than requested")
    return ReleaseRef(
        artifact_digest=prepared.image.artifact_digest,
        manifest_digest=prepared.image.manifest_digest,
        schema_digest=prepared.image.schema_digest,
        source_commit=prepared.source.source_commit,
    )


def parse_unit(value: str) -> UnitKey:
    """`MACHINE:HOME`; the home keeps any later colon (a Windows `C:\\...` home)."""
    machine, sep, home = value.partition(":")
    if not sep:
        raise ValueError(f"a unit is MACHINE:HOME, not {value!r}")
    return UnitKey(machine=machine, home=home)


def _exclusions(
    gateway: UnitKey,
    registered: Collection[tuple[str, str]],
    paused: Collection[str],
    exclude: tuple[str, ...],
    reason: str | None,
) -> tuple[Exclusion, ...]:
    if bool(exclude) != (reason is not None):
        raise ValueError("--exclude and --reason are given together")
    chosen = {parse_unit(value).order for value in exclude}
    if strangers := sorted(chosen - set(registered)):
        raise ValueError(f"excluded units are not registered: {strangers}")
    if gateway.order in chosen:
        raise ValueError("the gateway home runs the release; it cannot be excluded")
    exclusions: list[Exclusion] = []
    for machine, home in sorted(set(registered) - {gateway.order}):
        unit = UnitKey(machine=machine, home=home)
        if unit.order in chosen:
            recorded_by = f"operator:{getpass.getuser()}"
            exclusions.append(
                Exclusion(
                    unit=unit, reason="operator", recorded_by=recorded_by, detail=reason or ""
                )
            )
        elif machine in paused:
            exclusions.append(
                Exclusion(
                    unit=unit, reason="paused", recorded_by="request", detail="paused machine"
                )
            )
        else:
            raise ValueError(
                f"registered unit {unit.label} would take part: {NETWORKED_REFUSAL}; "
                f"exclude it with --exclude {unit.label} --reason ..."
            )
    return tuple(exclusions)


def _policy(
    home: Path,
    *,
    watch_s: int | None,
    alert_agent: int | None,
    alert_webhook_file: str | None,
    acknowledged_rejection: str | None,
) -> FleetPolicy:
    """The captured policy: the operator's choices over `FleetPolicy`'s defaults."""
    from cli.release_fleet.delivery import webhook_url

    route = AlertRoute(recipient_agent=alert_agent, webhook_file=alert_webhook_file)
    if route.webhook_file is not None:
        webhook_url(home, route.webhook_file)
    chosen: dict[str, object] = {"alert_route": route}
    if watch_s is not None:
        chosen["watch_s"] = watch_s
    if acknowledged_rejection is not None:
        chosen["acknowledged_rejection"] = UUID(acknowledged_rejection)
    return FleetPolicy.model_validate(chosen)


def _build_request(
    *,
    commit: str,
    receipt: Path | None,
    exclude: tuple[str, ...],
    reason: str | None,
    watch_s: int | None,
    alert_agent: int | None = None,
    alert_webhook_file: str | None = None,
    acknowledged_rejection: str | None = None,
) -> FleetRequest:
    from shared.cluster import registry_path
    from shared.machine import machine_name
    from shared.paths import ava_home
    from shared.start_inputs import configuration_digest

    require_commit_shape(commit)
    home = ava_home()
    candidate = _candidate_from_receipt(home, commit, receipt)
    found = current_release(home)
    if found is None:
        raise ValueError(
            "this home has no active release selection yet — run `ava cluster release adopt` first"
        )
    previous, _ = found
    if previous.selector == candidate.selector:
        raise ValueError("the prepared candidate is already this home's active release")
    gateway = UnitKey(machine=machine_name(), home=str(home))
    registered, _machines, paused = registered_units()
    policy = _policy(
        home,
        watch_s=watch_s,
        alert_agent=alert_agent,
        alert_webhook_file=alert_webhook_file,
        acknowledged_rejection=acknowledged_rejection,
    )
    request = FleetRequest(
        id=uuid4(),
        home=str(home),
        registry=str(registry_path()),
        created_at=datetime.now(UTC),
        machine=gateway.machine,
        previous=previous,
        candidate=candidate,
        executor=candidate,
        configuration_digest=configuration_digest(home),
        excluded=_exclusions(gateway, registered, paused, exclude, reason),
        policy=policy,
    )
    check_inventory(request, registered, paused)
    verify_pair(request)
    return request


def _write_request(out: Path, request: FleetRequest) -> None:
    encoded = (request.model_dump_json() + "\n").encode()
    try:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError(f"{out} already exists") from None
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def cmd_release_request(
    *,
    commit: str,
    out: Path,
    exclude: tuple[str, ...],
    reason: str | None,
    receipt: Path | None = None,
    watch_s: int | None = None,
    alert_agent: int | None = None,
    alert_webhook_file: str | None = None,
    acknowledged_rejection: str | None = None,
) -> int:
    try:
        request = _build_request(
            commit=commit,
            receipt=receipt,
            exclude=exclude,
            reason=reason,
            watch_s=watch_s,
            alert_agent=alert_agent,
            alert_webhook_file=alert_webhook_file,
            acknowledged_rejection=acknowledged_rejection,
        )
        _write_request(out, request)
    except (ValueError, OSError, RuntimeError) as exc:
        sys.stderr.write(f"release request refused: {exc}\n")
        return 2
    sys.stdout.write(
        json.dumps(
            {
                "request": str(out),
                "operation": str(request.path),
                "id": str(request.id),
                "excluded": [entry.unit.label for entry in request.excluded],
            }
        )
        + "\n"
    )
    return 0

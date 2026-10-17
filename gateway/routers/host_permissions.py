from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from base.host.permissions_helper.grants import checklist
from base.host.permissions_helper.grants.gate import GateReport
from gateway.schemas.host_permissions import (
    HostPermissionsPayload,
    HostPermissionsRequest,
    PermissionItemRow,
)
from services.desktop.permissions_helper.onboarding import GateService

router = APIRouter()


def gate_service() -> GateService:
    return GateService()


def _payload(report: GateReport) -> HostPermissionsPayload:
    data = checklist.payload(report)
    return HostPermissionsPayload(
        state=data["state"],
        headline=data["headline"],
        checked_at=data["checked_at"],
        diagnostic=data["diagnostic"],
        helper_reachable=data["helper_reachable"],
        required_total=data["required_total"],
        required_missing=data["required_missing"],
        items=[PermissionItemRow(**row) for row in data["items"]],
    )


@router.get(
    "/api/host/permissions",
    description=(
        "Read this host's macOS authorization matrix with a live probe and return\n"
        "the one-page checklist (why each grant matters, where the click goes, how\n"
        "to confirm it). `state` is complete / action_required / deferred /\n"
        "unavailable / not_applicable."
    ),
)
def get_host_permissions(
    gate: Annotated[GateService, Depends(gate_service)],
) -> HostPermissionsPayload:
    return _payload(gate.check())


@router.post(
    "/api/host/permissions/recheck",
    description=(
        "Probe again and return the same payload. The checklist's Re-check button\n"
        "uses this so an already-open page does not have to trust what it rendered."
    ),
)
def recheck_host_permissions(
    gate: Annotated[GateService, Depends(gate_service)],
) -> HostPermissionsPayload:
    return _payload(gate.check())


@router.post(
    "/api/host/permissions/request",
    description=(
        "Run the first-run pass: request every missing grant a dialog can still\n"
        "resolve, then re-read the matrix. Requires confirm_user_present -- a\n"
        "pending dialog blocks synthesized input machine-wide until answered."
    ),
)
def request_host_permissions(
    body: HostPermissionsRequest,
    gate: Annotated[GateService, Depends(gate_service)],
) -> HostPermissionsPayload:
    if not body.confirm_user_present:
        raise HTTPException(
            status_code=400,
            detail=(
                "confirm_user_present is required: raising permission dialogs needs a"
                " person at the machine to answer them"
            ),
        )
    return _payload(gate.onboard(interactive=True))


@router.post(
    "/api/host/permissions/defer",
    description=(
        'Record the user\'s "later". Nothing becomes granted: the host keeps its\n'
        "restricted state and the next start asks again."
    ),
)
def defer_host_permissions(
    gate: Annotated[GateService, Depends(gate_service)],
) -> HostPermissionsPayload:
    gate.defer()
    return _payload(gate.check())

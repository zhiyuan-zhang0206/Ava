from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from base.host.permissions_helper.grants import checklist
from base.host.permissions_helper.grants.gate import GateReport
from gateway.schemas.host_permissions import HostPermissionsPayload, PermissionItemRow
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
        "Request every grant a system dialog can still resolve, then re-read the\n"
        "matrix. This is the explicit path: an automatic pass asks only grants the\n"
        "user has never been asked about, and a denied one stays manual. macOS shows\n"
        "one dialog per bundle identity and answers later requests silently."
    ),
)
def request_host_permissions(
    gate: Annotated[GateService, Depends(gate_service)],
) -> HostPermissionsPayload:
    return _payload(gate.onboard(automatic=False))


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

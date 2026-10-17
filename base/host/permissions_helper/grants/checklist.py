from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from base.host.permissions_helper.grants.catalog import (
    Grant,
    PermissionItem,
    RequestMode,
    settings_url,
)
from base.host.permissions_helper.grants.gate import GateReport, GateState

_STATE_LABELS: dict[Grant, str] = {
    Grant.GRANTED: "granted",
    Grant.DENIED: "denied -- turn it on in System Settings",
    Grant.NOT_DETERMINED: "not requested yet",
    Grant.UNKNOWN: "not readable here",
}

_MODE_LABELS: dict[RequestMode, str] = {
    RequestMode.PROMPT: "a dialog can grant it",
    RequestMode.SYSTEM_SETTINGS: "System Settings only",
    RequestMode.LIVE_EVENT: "granted per app, on first use",
    RequestMode.REPORT_ONLY: "reported, never requested",
}

_HEADLINES: dict[GateState, str] = {
    GateState.COMPLETE: "All desktop permissions are in place.",
    GateState.ACTION_REQUIRED: "Some permissions are still missing.",
    GateState.DEFERRED: "Some permissions are still missing (you chose to do this later).",
    GateState.UNAVAILABLE: "The permissions helper did not answer, so nothing was checked.",
    GateState.NOT_APPLICABLE: "This host needs no macOS permissions.",
}


class ChecklistItemPayload(TypedDict):
    id: str
    title: str
    group: str
    state: str
    state_label: str
    mode: str
    why: str
    jump: str
    verify: str
    restart: str
    required: bool
    blocking: bool
    detail: str


class ChecklistPayload(TypedDict):
    state: str
    headline: str
    checked_at: str
    diagnostic: str
    helper_reachable: bool
    required_total: int
    required_missing: int
    items: list[ChecklistItemPayload]


@dataclass(frozen=True)
class ChecklistRow:
    id: str
    title: str
    group: str
    state: Grant
    state_label: str
    mode: str
    why: str
    jump: str
    verify: str
    restart: str
    required: bool
    blocking: bool = False
    detail: str = ""

    def to_payload(self) -> ChecklistItemPayload:
        return ChecklistItemPayload(
            id=self.id,
            title=self.title,
            group=self.group,
            state=self.state.value,
            state_label=self.state_label,
            mode=self.mode,
            why=self.why,
            jump=self.jump,
            verify=self.verify,
            restart=self.restart,
            required=self.required,
            blocking=self.blocking,
            detail=self.detail,
        )


def _restart_note(item: PermissionItem) -> str:
    if not item.helper_restart:
        return ""
    return (
        "Restart the permissions helper after granting it "
        "(`meiva permissions restart-helper`): macOS only re-reads this grant at process start."
    )


def rows(report: GateReport) -> tuple[ChecklistRow, ...]:
    return tuple(
        ChecklistRow(
            id=status.item.id,
            title=status.item.title,
            group=status.item.group,
            state=status.grant,
            state_label=_STATE_LABELS[status.grant],
            mode=_MODE_LABELS[status.item.mode],
            why=status.item.why,
            jump=settings_url(status.item),
            verify=status.item.verify,
            restart=_restart_note(status.item),
            required=status.item.required,
            blocking=status.blocks,
            detail=status.detail,
        )
        for status in report.items
    )


def headline(report: GateReport) -> str:
    return _HEADLINES[report.state]


def _row_lines(row: ChecklistRow) -> list[str]:
    mark = "-" if row.blocking else ("x" if row.state is Grant.GRANTED else " ")
    label = "" if row.required else " (optional)"
    lines = ["", f"[{mark}] {row.title}{label} -- {row.state_label}", f"    why:    {row.why}"]
    lines.append(f"    how:    {row.mode}")
    if not row.blocking:
        return lines
    lines.append(f"    open:   {row.jump}")
    lines.append(f"    verify: {row.verify}")
    if row.restart:
        lines.append(f"    after:  {row.restart}")
    return lines


def _count_line(checked: tuple[ChecklistRow, ...]) -> str:
    required = [row for row in checked if row.required]
    missing = [row for row in required if row.blocking]
    if not missing:
        return "Every required authorization is granted."
    return f"{len(missing)} of {len(required)} required authorizations are not granted."


def _recheck_lines() -> list[str]:
    return [
        "",
        "Re-check with `meiva permissions status`; grant what is still missing,",
        "then run `meiva permissions status` again -- nothing is assumed.",
    ]


def render_text(report: GateReport) -> str:
    lines = [headline(report), f"checked at {report.checked_at.isoformat(timespec='seconds')}"]
    if report.diagnostic:
        lines.append(report.diagnostic)
    checked = rows(report)
    if not checked:
        return "\n".join(lines)
    lines.append(_count_line(checked))
    for row in checked:
        lines.extend(_row_lines(row))
    if report.state in (GateState.ACTION_REQUIRED, GateState.DEFERRED):
        lines.extend(_recheck_lines())
    return "\n".join(lines)


def payload(report: GateReport) -> ChecklistPayload:
    checked = rows(report)
    return ChecklistPayload(
        state=report.state.value,
        headline=headline(report),
        checked_at=report.checked_at.isoformat(),
        diagnostic=report.diagnostic,
        helper_reachable=report.helper_reachable,
        required_total=sum(1 for row in checked if row.required),
        required_missing=sum(1 for row in checked if row.blocking),
        items=[row.to_payload() for row in checked],
    )

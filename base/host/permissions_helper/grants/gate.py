from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from base.host.permissions_helper.grants.catalog import (
    CATALOG,
    ITEMS_BY_ID,
    REQUIRED_ITEMS,
    Grant,
    PermissionItem,
    RequestMode,
)
from base.host.permissions_helper.grants.probe import ProbeResult
from base.paths import meiva_home

RECORD_NAME = "permissions_onboarding.json"
RECORD_VERSION = 1

BLOCKING_GRANTS = frozenset({Grant.DENIED, Grant.NOT_DETERMINED, Grant.STALE})


class GateState(StrEnum):
    COMPLETE = "complete"
    ACTION_REQUIRED = "action_required"
    DEFERRED = "deferred"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class ItemStatus:
    item: PermissionItem
    grant: Grant
    detail: str = ""

    @property
    def blocks(self) -> bool:
        return self.item.required and self.grant in BLOCKING_GRANTS


@dataclass(frozen=True)
class GateReport:
    state: GateState
    checked_at: datetime
    items: tuple[ItemStatus, ...]
    diagnostic: str = ""
    helper_reachable: bool = True
    supports_request: bool = False

    def status_for(self, item_id: str) -> ItemStatus | None:
        for status in self.items:
            if status.item.id == item_id:
                return status
        return None

    def grant_for(self, item_id: str) -> Grant:
        status = self.status_for(item_id)
        return status.grant if status is not None else Grant.UNKNOWN

    @property
    def missing(self) -> tuple[ItemStatus, ...]:
        return tuple(status for status in self.items if status.blocks)

    @property
    def complete(self) -> bool:
        return self.state is GateState.COMPLETE

    @property
    def restricted(self) -> bool:
        return self.state in (GateState.ACTION_REQUIRED, GateState.DEFERRED, GateState.UNAVAILABLE)

    def prompt_now(self) -> tuple[PermissionItem, ...]:
        return tuple(
            status.item
            for status in self.items
            if status.item.mode is RequestMode.PROMPT and status.grant is Grant.NOT_DETERMINED
        )

    def requestable(self) -> tuple[PermissionItem, ...]:
        return tuple(
            status.item
            for status in self.items
            if status.item.mode is RequestMode.PROMPT
            and status.grant in (Grant.NOT_DETERMINED, Grant.DENIED)
        )

    def manual(self) -> tuple[PermissionItem, ...]:
        rows: list[PermissionItem] = []
        for status in self.items:
            if not status.item.required or status.grant is Grant.GRANTED:
                continue
            if status.item.mode is RequestMode.SYSTEM_SETTINGS or (
                status.item.mode is RequestMode.PROMPT and status.grant is Grant.DENIED
            ):
                rows.append(status.item)
        return tuple(rows)


# meiva:link base/host/permissions_helper/grants/docs/grants.meiva.okf.md
def _detail(result: ProbeResult, item: PermissionItem) -> str:
    if result.grant_for(item) is Grant.STALE:
        return (
            "macOS already records this grant; the running helper has not read it yet, so it"
            " cannot use it until it restarts."
        )
    return ""


# meiva:note
# A host state is computed from the probe of the same call. The record under $MEIVA_HOME stores the last observation and the user's deferral flag; it is written from a probe and never read as evidence, so a cached 'granted' can neither hide a revoked grant nor invent one.
# meiva:link base/host/permissions_helper/grants/docs/grants.meiva.okf.md
def evaluate(
    result: ProbeResult, *, deferred: bool = False, now: datetime | None = None
) -> GateReport:
    checked_at = now or datetime.now(UTC)
    if not result.helper_reachable:
        items = tuple(ItemStatus(item, Grant.UNKNOWN, "helper unreachable") for item in CATALOG)
        return GateReport(
            state=GateState.UNAVAILABLE,
            checked_at=checked_at,
            items=items,
            diagnostic=result.diagnostic,
            helper_reachable=False,
            supports_request=False,
        )
    items = tuple(
        ItemStatus(item, result.grant_for(item), _detail(result, item)) for item in CATALOG
    )
    report = GateReport(
        state=GateState.COMPLETE,
        checked_at=checked_at,
        items=items,
        diagnostic=result.diagnostic,
        supports_request=result.supports_request,
    )
    if report.missing:
        return GateReport(
            state=GateState.DEFERRED if deferred else GateState.ACTION_REQUIRED,
            checked_at=checked_at,
            items=items,
            diagnostic=result.diagnostic,
            supports_request=result.supports_request,
        )
    return report


def not_applicable(now: datetime | None = None) -> GateReport:
    return GateReport(
        state=GateState.NOT_APPLICABLE,
        checked_at=now or datetime.now(UTC),
        items=(),
        diagnostic="There are no macOS authorizations to check on this host.",
        supports_request=False,
    )


@dataclass(frozen=True)
class OnboardingRecord:
    checked_at: datetime
    state: GateState
    deferred: bool
    grants: dict[str, Grant]

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": RECORD_VERSION,
                "checked_at": self.checked_at.isoformat(),
                "state": self.state.value,
                "deferred": self.deferred,
                "grants": {item_id: grant.value for item_id, grant in sorted(self.grants.items())},
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> OnboardingRecord:
        data = json.loads(text)
        if data.get("version") != RECORD_VERSION:
            raise ValueError(f"unsupported onboarding record version {data.get('version')!r}")
        return cls(
            checked_at=datetime.fromisoformat(data["checked_at"]),
            state=GateState(data["state"]),
            deferred=bool(data["deferred"]),
            grants={item_id: Grant(value) for item_id, value in data["grants"].items()},
        )

    @classmethod
    def from_report(cls, report: GateReport, *, deferred: bool) -> OnboardingRecord:
        return cls(
            checked_at=report.checked_at,
            state=report.state,
            deferred=deferred,
            grants={status.item.id: status.grant for status in report.items},
        )


def record_path() -> Path:
    return meiva_home() / RECORD_NAME


def read_record(path: Path | None = None) -> OnboardingRecord | None:
    target = path or record_path()
    try:
        return OnboardingRecord.from_json(target.read_text())
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError):
        return None


def write_record(record: OnboardingRecord, path: Path | None = None) -> None:
    target = path or record_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(record.to_json())


# meiva:note
# Recording 'later' keeps the host in the restricted family: the state becomes deferred, complete stays false, and the next gate run asks again. A complete run clears the flag.
# meiva:link base/host/permissions_helper/grants/docs/grants.meiva.okf.md
def mark_deferred(*, now: datetime | None = None, path: Path | None = None) -> OnboardingRecord:
    record = read_record(path)
    if record is None:
        record = OnboardingRecord(
            checked_at=now or datetime.now(UTC),
            state=GateState.DEFERRED,
            deferred=True,
            grants={},
        )
    else:
        record = OnboardingRecord(
            checked_at=record.checked_at,
            state=GateState.DEFERRED,
            deferred=True,
            grants=dict(record.grants),
        )
    write_record(record, path)
    return record


def remember(report: GateReport, *, deferred: bool, path: Path | None = None) -> OnboardingRecord:
    record = OnboardingRecord.from_report(report, deferred=deferred and not report.complete)
    write_record(record, path)
    return record


def required_ids() -> tuple[str, ...]:
    return tuple(item.id for item in REQUIRED_ITEMS)


def item_for(item_id: str) -> PermissionItem:
    return ITEMS_BY_ID[item_id]

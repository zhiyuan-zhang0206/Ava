"""Durable root birth intent and observed native children.

An unfinished record blocks another root generation. It is evidence to reconcile,
never permission to signal a PID or assume an unacknowledged spawn did not occur.
This does not establish closure of independently registered execution domains.

Reconciliation (task #4872, route C) proves a record releasable: every recorded
birth is re-verified against its captured native identity, and the unit's
recorded process group must have no member left (zombies included, the closure
the stop path reads). One unproven fact retains the record and names the
evidence; nothing here signals. The pass runs on the cold-start custody gate
(`require_clear`), before a fresh spawn reuses a unit's record slot, and once
per health round. A repeatable pass reports a retained record on first sight
and on evidence change only; a release always reports.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Mapping, MutableMapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

import psutil

from base.native_process.group_closure import group_empty, group_members
from base.native_process.ownership import OwnedProcess

_log = logging.getLogger(__name__)

_RECORD_VERSION = 2

_SHOWN_MEMBERS = 8
"""Members named in one refusal or event before the remainder is counted.

A message-density choice, not a tuning knob (task #3696 exception inventory):
the `+N more` remainder is always carried, so the cap cannot change any
decision — what would change it is a different evidence-line layout.
"""


def require_clear(run_dir: Path) -> None:
    """Reconcile every custody record, then refuse a cold startup while any remains.

    A record whose every recorded birth is gone and whose process group is
    empty is cleared here, so a cold start needs no operator force for it; a
    record that keeps one unproven fact refuses the start by naming its
    reconcile steps, force path and evidence path.
    """
    outcomes = reconcile_all(run_dir)
    directory = run_dir / "custody"
    if not directory.exists() or not any(directory.iterdir()):
        return
    retained = [outcome for outcome in outcomes if outcome.decision == "retained"]
    named = {f"{outcome.unit}.json" for outcome in retained}
    unknown = sorted(entry.name for entry in directory.iterdir() if entry.name not in named)
    raise custody_refusal(directory, retained, unknown)


def _flush_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ServiceCustody:
    """One unit's pending birth, retained until positively observed cleanup."""

    def __init__(self, run_dir: Path, unit: str) -> None:
        directory = run_dir / "custody"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / f"{unit}.json"
        self._body: dict[str, object] = {
            "version": _RECORD_VERSION,
            "unit": unit,
            "stage": "spawning",
        }
        self._expected = json.dumps(self._body)
        _flush_directory(directory.parent)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(self._expected)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            _flush_directory(directory)

    def retain(self, identities: set[OwnedProcess], group: int) -> None:
        """Persist captured births before stopping any of those processes.

        `group` is the unit's process group — the leader's pid, the number a
        reconcile pass reads to prove closure once every recorded birth is gone.
        """
        self._require_unchanged()
        self._body = self._body | {
            "stage": "running",
            "processes": [asdict(item) for item in sorted(identities, key=lambda p: p.pid)],
            "group": group,
        }
        from base.host.atomic_io import write_text_atomic

        self._expected = json.dumps(self._body)
        write_text_atomic(self.path, self._expected, mode=0o600, sync_parent=True)

    def clear(self) -> None:
        """Remove only this owner's record after completed native cleanup."""
        self._require_unchanged()
        self.path.unlink()
        _flush_directory(self.path.parent)

    def _require_unchanged(self) -> None:
        if self.path.is_symlink() or self.path.read_text() != self._expected:
            raise RuntimeError(f"native custody changed; refusing mutation: {self.path}")


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    """One record's reconcile result; `decision` is `released` or `retained`."""

    unit: str
    decision: Literal["released", "retained"]
    checked: int
    found: int
    evidence: str


def record_paths(run_dir: Path) -> list[Path]:
    """Every custody record file under `run_dir`, in name order."""
    directory = run_dir / "custody"
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if path.suffix == ".json")


def reconcile_all(
    run_dir: Path, *, reports: MutableMapping[str, str] | None = None
) -> list[ReconcileOutcome]:
    """Reconcile every custody record under `run_dir`, in name order.

    `reports` makes repeated passes change-driven: a retained outcome whose
    fingerprint matches the unit's last report is served without a repeat
    event (task #4872, C-1). One-shot callers omit it and report every
    examined record.
    """
    outcomes: list[ReconcileOutcome] = []
    for path in record_paths(run_dir):
        outcome = reconcile_record(run_dir, path.stem, reports=reports)
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes


def reconcile_record(
    run_dir: Path, unit: str, *, reports: MutableMapping[str, str] | None = None
) -> ReconcileOutcome | None:
    """Reconcile one unit's record; None when it holds no record.

    Total by construction: a record it cannot prove releasable is retained
    with the evidence that kept it, and every examined record reports a
    `custody_reconcile` event — released or retained (task #4872, C-1);
    under `reports`, an unchanged retained outcome reports once, not again.
    """
    path = run_dir / "custody" / f"{unit}.json"
    if not path.exists() and not path.is_symlink():
        return None
    outcome = _reconcile(path, unit)
    _report(outcome, reports)
    return outcome


def _reconcile(path: Path, unit: str) -> ReconcileOutcome:
    if path.is_symlink():
        return _outcome(unit, "retained", 0, 0, "record path is a symlink; refusing to reconcile")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _outcome(unit, "retained", 0, 0, f"record not readable ({type(exc).__name__})")
    fields = _record_fields(parsed, unit)
    if isinstance(fields, str):
        return _outcome(unit, "retained", 0, 0, f"record not understood: {fields}")
    births, group = fields
    return _decide(path, unit, births, group)


def _decide(
    path: Path, unit: str, births: list[OwnedProcess], group: int | None
) -> ReconcileOutcome:
    """What the verified births and the recorded group prove about one record."""
    statuses = [(birth.pid, _birth_status(birth)) for birth in births]
    checked = len(statuses)
    found = sum(1 for _pid, status in statuses if status == "live")
    digest = ", ".join(f"pid {pid} {status}" for pid, status in statuses)
    unreadable = [pid for pid, status in statuses if status == "unknown"]
    if unreadable:
        return _outcome(
            unit, "retained", checked, found, f"unverifiable recorded births {unreadable}; {digest}"
        )
    if found:
        return _outcome(
            unit,
            "retained",
            checked,
            found,
            f"{found} of {checked} recorded births still run ({digest})",
        )
    if group is None:
        return _outcome(
            unit,
            "retained",
            checked,
            found,
            f"every recorded birth is gone ({digest}), but no process group is recorded, "
            "so closure cannot be proven",
        )
    return _close_or_retain(path, unit, checked, found, digest, group)


def _close_or_retain(
    path: Path, unit: str, checked: int, found: int, digest: str, group: int
) -> ReconcileOutcome:
    """Release under a closed group, or keep the record with what blocked it."""
    try:
        closed = group_empty(group)
    except OSError as exc:
        return _outcome(
            unit, "retained", checked, found, f"process group {group} unreadable ({exc}); {digest}"
        )
    if not closed:
        return _outcome(
            unit,
            "retained",
            checked,
            found,
            f"process group {group} still holds {_members(group)}; {digest}",
        )
    try:
        path.unlink()
        _flush_directory(path.parent)
    except OSError as exc:
        return _outcome(
            unit,
            "retained",
            checked,
            found,
            f"record could not be removed ({exc}) although every birth is gone and group "
            f"{group} is empty",
        )
    return _outcome(
        unit,
        "released",
        checked,
        found,
        f"every recorded birth is gone ({digest}); process group {group} empty",
    )


def _record_fields(parsed: object, unit: str) -> tuple[list[OwnedProcess], int | None] | str:
    """The verified fields of one record, or the reason it is not understood."""
    if not isinstance(parsed, dict):
        return "not a JSON object"
    values = cast("Mapping[str, object]", parsed)
    if values.get("unit") != unit:
        return "record names a different unit"
    version = values.get("version")
    if isinstance(version, bool) or version not in (1, _RECORD_VERSION):
        return f"version {version!r}"
    if values.get("stage") != "running":
        return f"no acknowledged generation (stage {values.get('stage')!r})"
    births = _record_births(values)
    if isinstance(births, str):
        return births
    group = _record_group(values, version)
    if isinstance(group, str):
        return group
    return births, group


def _record_births(values: Mapping[str, object]) -> list[OwnedProcess] | str:
    """The record's verified birth entries, or the reason one is not understood."""
    processes = values.get("processes")
    if not isinstance(processes, list) or not processes:
        return "no recorded births"
    births: list[OwnedProcess] = []
    for item in cast("list[object]", processes):
        entry = _birth_entry(item)
        if isinstance(entry, str):
            return entry
        births.append(entry)
    return births


def _record_group(values: Mapping[str, object], version: object) -> int | str | None:
    """The record's group number (version 2 only), or why it is malformed."""
    if version != _RECORD_VERSION:
        return None
    raw_group = values.get("group")
    if raw_group is None:
        return None
    if isinstance(raw_group, bool) or not isinstance(raw_group, int) or raw_group <= 0:
        return f"malformed group {raw_group!r}"
    return raw_group


def _birth_entry(item: object) -> OwnedProcess | str:
    if not isinstance(item, dict):
        return "malformed process entry"
    values = cast("Mapping[str, object]", item)
    pid = values.get("pid")
    birth = values.get("birth")
    starttime = values.get("starttime")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return f"malformed pid {pid!r}"
    if isinstance(birth, bool) or not isinstance(birth, (int, float)) or not math.isfinite(birth):
        return f"malformed birth for pid {pid}"
    if starttime is not None and (
        isinstance(starttime, bool) or not isinstance(starttime, int) or starttime <= 0
    ):
        return f"malformed starttime for pid {pid}"
    return OwnedProcess(pid, float(birth), starttime)


def _birth_status(birth: OwnedProcess) -> str:
    """One recorded birth against its captured native identity (task #4872, C-5).

    `live`: a running process of that exact birth — SIGSTOP/T included, since a
    stopped process is not gone. `gone`: no such process. `reused`: the PID now
    names another birth, so the recorded one exited. `zombie`: dead, not yet
    reaped. `unknown`: the identity could not be read — never treated as gone.
    """
    try:
        process = psutil.Process(birth.pid)
        matches = birth.birth_matches(process)
    except psutil.NoSuchProcess:
        return "gone"
    except (psutil.Error, RuntimeError):
        return "unknown"
    if not matches:
        return "reused"
    try:
        status = process.status()
    except psutil.NoSuchProcess:
        return "gone"
    except psutil.Error:
        return "unknown"
    if status in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
        return "zombie"
    return "live"


def _members(group: int) -> str:
    try:
        members = group_members(group)
    except OSError as exc:
        return f"members root cannot list ({exc})"
    shown = ", ".join(str(pid) for pid in members[:_SHOWN_MEMBERS])
    if len(members) <= _SHOWN_MEMBERS:
        return f"[{shown}]"
    return f"[{shown}, +{len(members) - _SHOWN_MEMBERS} more]"


def _outcome(
    unit: str, decision: Literal["released", "retained"], checked: int, found: int, evidence: str
) -> ReconcileOutcome:
    return ReconcileOutcome(
        unit=unit, decision=decision, checked=checked, found=found, evidence=evidence
    )


def _report(outcome: ReconcileOutcome, reports: MutableMapping[str, str] | None) -> None:
    """Report one outcome under the caller's policy.

    One-shot passes (`reports` absent) report every examined record; a
    repeatable pass reports a release always and a retained outcome only when
    its fingerprint differs from the unit's last report (task #4872, C-1).
    """
    if reports is None:
        _emit(outcome)
        return
    if outcome.decision == "released":
        reports.pop(outcome.unit, None)
        _emit(outcome)
        return
    fingerprint = _fingerprint(outcome)
    if reports.get(outcome.unit) != fingerprint:
        reports[outcome.unit] = fingerprint
        _emit(outcome)


def _fingerprint(outcome: ReconcileOutcome) -> str:
    """The identity of one report: what a repeat must match to stay silent."""
    return f"{outcome.decision}|{outcome.checked}|{outcome.found}|{outcome.evidence}"


def _emit(outcome: ReconcileOutcome) -> None:
    """Fan one reconcile outcome out as a `custody_reconcile` audit event (task #4872, C-1)."""
    try:
        from base.log import logger

        emit = logger.warning if outcome.decision == "retained" else logger.info
        emit(
            "custody reconcile: unit {unit} {decision} ({checked} checked, {found} found) — "
            "{evidence}",
            event="custody_reconcile",
            unit=outcome.unit,
            checked=outcome.checked,
            found=outcome.found,
            decision=outcome.decision,
            evidence=outcome.evidence,
        )
    except Exception:
        # The decision is already durable (record cleared or kept); a fan-out
        # failure must never turn it into an exception.
        _log.exception("custody reconcile: event fan-out failed for unit %s", outcome.unit)


def custody_refusal(
    directory: Path, retained: list[ReconcileOutcome], unknown: list[str]
) -> RuntimeError:
    """The refusal naming reconcile steps, force path and evidence path (task #4872, C-2)."""
    parts = [f"root service custody requires reconciliation: {directory}"]
    for outcome in retained:
        path = directory / f"{outcome.unit}.json"
        parts.append(
            f"unit {outcome.unit}: retained — {outcome.evidence}. Reconcile steps: every "
            "recorded birth was re-verified by native identity (PID and birth; Linux start "
            "ticks), then the unit's process group was read for members; this record keeps a "
            f"fact that could not be proven gone. Force path: once no process of unit "
            f"{outcome.unit} remains, move {path} aside and retry — reconciliation never "
            "signals, and a moved-aside record with a recorded birth still live is refused. "
            f"Evidence path: {path}."
        )
    if unknown:
        parts.append(
            f"unrecognized entries in the custody directory: {', '.join(unknown)} — inspect "
            "and settle them, then retry."
        )
    return RuntimeError(" ".join(parts))

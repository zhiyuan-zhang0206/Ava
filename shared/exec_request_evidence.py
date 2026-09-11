"""Incarnation-attributed classification of leftover exec request envelopes.

A ``run/exec/<agent_id>/req-*.json`` envelope is the crash-stable half of one
disposable exec run: it is written before the child starts and removed only
after the exact process domain, root reap and output reader settle, so a
survivor means uncertain cleanup and the recovery paths refuse (issue #2157).

That refusal must still be judged against the incarnation that wrote the
envelope. An envelope attributed to a superseded incarnation whose host is gone
cannot fence an unrelated newer lifecycle forever: the only part of a dead
host's domain that can outlive it is its exec child, and a live child leaves
process evidence. A request is therefore quarantined only when every leg proves
it disposable:

- the envelope parses and carries its exact incarnation attribution;
- no live process references the request — the direct child and every
  env-inheriting descendant carry ``AVA_EXEC_REQUEST_FILE``, and a process
  whose environment this kernel will not show is never excluded while it looks
  like an ``agent.exec_child`` root born inside the request's own lifetime;
- the row's stored host identity, when one exists, is not a live process — a
  reused PID means the recorded boot ended, never that a replacement is the old
  host (the same identity check exec-owner recovery uses).

Everything else is retained with diagnostics naming the file, its attribution
and the disposition commands. Stale evidence is quarantined, never deleted: the
files move to ``$AVA_HOME/quarantined-exec-requests/<reason>-<stamp>/<agent_id>/``
beside a JSON receipt recording what was proven and why.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import psutil

from shared.db_transaction import write_transaction
from shared.exec_owner_recovery import process_ended
from shared.incarnation_resources import (
    IncarnationResources,
    ResourceEvidenceError,
    decode_resources,
)
from shared.log import logger
from shared.paths import exec_run_dir, quarantined_exec_requests_dir
from shared.runtime_incarnation import RuntimeIncarnation

# The envelope protocol's own ceiling (agent/graph/_exec_protocol.py). The
# typed state snapshot rides as one base64 field, so a legitimate envelope is
# parsed whole; anything larger is refused as unattributable evidence.
_MAX_ENVELOPE_BYTES = 64 * 1024 * 1024
_REQUEST_VERSION = 1

# Birth window for a would-be exec child. The request is written immediately
# before the spawn, and the child hard-exits at (timeout + parent kill grace +
# watchdog margin) — these slacks absorb every constant in that chain, so the
# window stays a superset of any real child's lifetime. Both bounds only ever
# add retention.
_BIRTH_FLOOR_SLACK_S = 5.0
_CHILD_LIFETIME_SLACK_S = 60.0

_EXEC_CHILD_MODULE = "agent.exec_child"
_REQUEST_REFERENCE_ENV = "AVA_EXEC_REQUEST_FILE"


class Verdict(StrEnum):
    """What the evidence proves about one request envelope."""

    LIVE = "live"  # a live process cannot be excluded from the request's domain
    STALE = "stale"  # attributed, unreferenced, and its host is provably gone
    UNKNOWN = "unknown"  # unattributable or unreadable: retained conservatively


class HostState(StrEnum):
    """The row's stored host identity, when it carries one."""

    ABSENT = "absent"  # no stored identity to check
    ENDED = "ended"  # the stored identity's exact process ended
    ALIVE = "alive"  # the stored identity's process cannot be shown ended
    UNREADABLE = "unreadable"  # stored evidence exists but does not decode


@dataclass(frozen=True)
class HostEvidence:
    """One decoded host-identity leg, shared by every request of an agent."""

    state: HostState
    detail: str


@dataclass(frozen=True)
class RequestEvidence:
    """One request envelope and what was proven about it."""

    agent_id: int
    path: Path
    verdict: Verdict
    incarnation: RuntimeIncarnation | None
    mtime: float
    live_pids: tuple[int, ...]
    detail: str

    @property
    def retained(self) -> bool:
        """True when the evidence must keep deferring recovery."""
        return self.verdict is not Verdict.STALE

    def describe(self) -> str:
        """A one-line diagnostic: file, attribution, proof and refusal."""
        owner = "unattributed" if self.incarnation is None else str(self.incarnation.owner)
        pids = ",".join(str(pid) for pid in self.live_pids) if self.live_pids else "none"
        return f"{self.path} [{self.verdict.value}] owner={owner} live_pids={pids}: {self.detail}"


@dataclass(frozen=True)
class QuarantinedEvidence:
    """One request envelope moved into the quarantine, source and target."""

    entry: RequestEvidence
    destination: Path


@dataclass(frozen=True)
class QuarantineReport:
    """The outcome of one agent's classification and quarantine pass."""

    agent_id: int
    event_dir: Path | None
    quarantined: tuple[QuarantinedEvidence, ...]
    retained: tuple[RequestEvidence, ...]


def request_paths(agent_id: int) -> tuple[Path, ...]:
    """Every request envelope currently present for one agent."""
    return tuple(sorted((exec_run_dir() / str(agent_id)).glob("req-*.json")))


def stored_host(resources: object) -> HostEvidence:
    """Decode the row's stored host identity, when it carries one.

    ``incarnation_resources`` is the only durable record of the host process
    itself. Absent (NULL, a birth marker, or a set without a host identity) is
    no proof either way — the callers establish that premise; a live identity
    vetoes every verdict, and an identity whose PID was reused means the
    recorded boot ended, never that the replacement is the old host.
    """
    if resources is None:
        return HostEvidence(HostState.ABSENT, "no stored host identity")
    try:
        evidence = decode_resources(resources)
    except (ResourceEvidenceError, ValueError) as exc:
        return HostEvidence(HostState.UNREADABLE, f"stored resource evidence is unreadable: {exc}")
    if not isinstance(evidence, IncarnationResources) or evidence.host_process is None:
        return HostEvidence(HostState.ABSENT, "no stored host identity")
    identity = evidence.host_process
    if process_ended(identity):
        return HostEvidence(HostState.ENDED, f"stored host process {identity.pid} ended")
    return HostEvidence(
        HostState.ALIVE, f"stored host process {identity.pid} is not provably ended"
    )


def live_domain_pids(path: Path, *, born_from: float, born_before: float) -> tuple[int, ...]:
    """PIDs that cannot be excluded as live members of this request's domain.

    Strong proof: a readable environment naming this exact request — the direct
    child and every env-inheriting descendant. Processes this kernel will not
    show an environment for are never excluded while they look like an
    ``agent.exec_child`` root born inside the request's own lifetime window; a
    readable non-match is excluded the same way. Unreadable is never absence.
    """
    target = os.path.realpath(path)
    found: set[int] = set()
    for process in psutil.process_iter(["pid", "status", "cmdline"]):
        pid = process.info["pid"]
        if pid == os.getpid() or process.info["status"] in {
            psutil.STATUS_DEAD,
            psutil.STATUS_ZOMBIE,
        }:
            continue
        try:
            environment = process.environ()
        except psutil.NoSuchProcess:
            continue
        except (psutil.AccessDenied, psutil.ZombieProcess, OSError):
            environment = None
        if environment is not None:
            raw = environment.get(_REQUEST_REFERENCE_ENV)
            if raw is not None and os.path.realpath(raw) == target:
                found.add(pid)
                continue
        if not _is_exec_child_argv(cast("list[str] | None", process.info["cmdline"])):
            continue
        if environment is None:
            found.add(pid)
            continue
        try:
            birth = process.create_time()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            found.add(pid)
            continue
        if born_from <= birth <= born_before:
            found.add(pid)
    return tuple(sorted(found))


def classify_request(
    path: Path,
    *,
    agent_id: int,
    incumbent: RuntimeIncarnation | None,
    host: HostEvidence,
) -> RequestEvidence:
    """Classify one request envelope against its attribution and process proof.

    ``incumbent`` is the incarnation the row still names — the retired owner in
    both recovery paths; it is reported, not trusted. The callers own the
    premise that this host ended (exclusive boot / absent host), and the stored
    host identity vetoes the verdict whenever it contradicts that premise.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        return RequestEvidence(
            agent_id,
            path,
            Verdict.UNKNOWN,
            None,
            time.time(),
            (),
            f"request envelope disappeared during classification: {exc}",
        )
    incarnation, refusal, timeout_s = _read_attribution(path, agent_id)
    live = live_domain_pids(
        path,
        born_from=mtime - _BIRTH_FLOOR_SLACK_S,
        born_before=mtime + max(timeout_s, 0.0) + _CHILD_LIFETIME_SLACK_S,
    )
    if live:
        return RequestEvidence(
            agent_id,
            path,
            Verdict.LIVE,
            incarnation,
            mtime,
            live,
            "live process(es) cannot be excluded from this request's exec domain",
        )
    if refusal is not None:
        return RequestEvidence(agent_id, path, Verdict.UNKNOWN, None, mtime, (), refusal)
    assert incarnation is not None  # noqa: S101 — refusal covers every unattributed envelope
    if host.state in {HostState.ALIVE, HostState.UNREADABLE}:
        return RequestEvidence(agent_id, path, Verdict.UNKNOWN, incarnation, mtime, (), host.detail)
    reached = "the retired incumbent" if incarnation == incumbent else "a superseded incarnation"
    return RequestEvidence(
        agent_id,
        path,
        Verdict.STALE,
        incarnation,
        mtime,
        (),
        f"attributed to {reached} ({incarnation.owner}); {host.detail}; no live process reference",
    )


def survey(
    agent_id: int,
    *,
    incumbent: RuntimeIncarnation | None,
    resources: object,
) -> tuple[RequestEvidence, ...]:
    """Classify every request envelope of one agent without touching them."""
    host = stored_host(resources)
    return tuple(
        classify_request(path, agent_id=agent_id, incumbent=incumbent, host=host)
        for path in request_paths(agent_id)
    )


def quarantine_stale(
    agent_id: int,
    *,
    incumbent: RuntimeIncarnation | None,
    resources: object,
    reason: str,
) -> QuarantineReport:
    """Move provably stale envelopes aside, preserving them with a receipt.

    Live or unattributable evidence is returned retained; a file that already
    vanished (a settled run, or a racing classifier) is discharged silently,
    while any other move failure keeps that entry retained so the caller still
    refuses. This never deletes evidence, never signals a process and never
    touches the database: the lifecycle transition stays with the caller.
    """
    host = stored_host(resources)
    entries = tuple(
        classify_request(path, agent_id=agent_id, incumbent=incumbent, host=host)
        for path in request_paths(agent_id)
    )
    stale = tuple(entry for entry in entries if not entry.retained)
    retained = tuple(entry for entry in entries if entry.retained)
    if not stale:
        return QuarantineReport(agent_id, None, (), retained)
    committed = _commit(agent_id, stale, reason=reason)
    return QuarantineReport(
        agent_id, committed.event_dir, committed.quarantined, retained + committed.retained
    )


def disposition_hint(agent_id: int) -> str:
    """The exact commands an operator can run against one agent's evidence."""
    executable = sys.executable
    return (
        f"inspect: {executable} -m shared.exec_request_evidence --agent {agent_id}; "
        f"quarantine after review: {executable} -m shared.exec_request_evidence "
        f"--agent {agent_id} --quarantine <file> [--force]"
    )


def _read_attribution(
    path: Path, agent_id: int
) -> tuple[RuntimeIncarnation | None, str | None, float]:
    """Read (attribution, refusal reason, declared timeout) from one envelope."""
    try:
        size = path.stat().st_size
        if size > _MAX_ENVELOPE_BYTES:
            return None, f"envelope is {size} bytes, over the {_MAX_ENVELOPE_BYTES} ceiling", 0.0
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"envelope is unreadable: {exc}", 0.0
    if not isinstance(parsed, dict):
        return None, "envelope is not a JSON object", 0.0
    envelope = cast("dict[str, Any]", parsed)
    version = envelope.get("v")
    if version != _REQUEST_VERSION:
        return None, f"envelope version {version!r} != {_REQUEST_VERSION}", 0.0
    named = envelope.get("agent_id")
    if named != agent_id:
        return None, f"envelope names agent {named!r}, not {agent_id}", 0.0
    timeout = envelope.get("timeout_s")
    timeout_s = float(timeout) if isinstance(timeout, int | float) else 0.0
    identity = envelope.get("incarnation")
    if not isinstance(identity, dict):
        return None, "envelope carries no incarnation", timeout_s
    fields = cast("dict[str, Any]", identity)
    try:
        incarnation = RuntimeIncarnation(
            agent_id, UUID(str(fields["generation"])), UUID(str(fields["owner"]))
        )
    except (KeyError, ValueError) as exc:
        return None, f"envelope incarnation is malformed: {exc}", timeout_s
    return incarnation, None, timeout_s


def _is_exec_child_argv(argv: Sequence[str] | None) -> bool:
    """True when argv is the isolated `-m agent.exec_child` launch shape."""
    if argv is None:
        return False
    return any(
        argv[index] == "-m" and argv[index + 1] == _EXEC_CHILD_MODULE
        for index in range(len(argv) - 1)
    )


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(0o700)
    return path


def _free_name(path: Path) -> Path:
    """A collision-free sibling of `path`; identical names never clobber."""
    candidate = path
    counter = 0
    while candidate.exists():
        counter += 1
        candidate = path.with_name(f"{path.stem}.{counter}{path.suffix}")
    return candidate


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")


def _slug(reason: str) -> str:
    slug = "-".join(
        part for part in "".join(c if c.isalnum() else " " for c in reason.lower()).split()
    )
    return slug or "manual"


def _replaced(entry: RequestEvidence, detail: str) -> RequestEvidence:
    return RequestEvidence(
        entry.agent_id,
        entry.path,
        entry.verdict,
        entry.incarnation,
        entry.mtime,
        entry.live_pids,
        detail,
    )


def _payloaded(entry: RequestEvidence) -> dict[str, Any]:
    incarnation = entry.incarnation
    return {
        "source": str(entry.path),
        "destination": None,
        "verdict": entry.verdict.value,
        "generation": None if incarnation is None else str(incarnation.generation),
        "owner": None if incarnation is None else str(incarnation.owner),
        "mtime": datetime.fromtimestamp(entry.mtime, UTC).isoformat(),
        "live_pids": list(entry.live_pids),
        "detail": entry.detail,
    }


def _write_receipt(
    path: Path, agent_id: int, reason: str, moved: Sequence[QuarantinedEvidence]
) -> None:
    """The durable, human-readable record of one quarantine pass."""
    entries: list[dict[str, Any]] = []
    for item in moved:
        payload = _payloaded(item.entry)
        payload["destination"] = str(item.destination)
        entries.append(payload)
    receipt: dict[str, Any] = {
        "agent_id": agent_id,
        "reason": reason,
        "quarantined_at": datetime.now(UTC).isoformat(),
        "entries": entries,
    }
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _row_identity(agent_id: int) -> tuple[RuntimeIncarnation | None, object]:
    """The row's current incarnation and stored resources, for the CLI."""
    with write_transaction() as conn:
        row = conn.execute(
            "SELECT runtime_generation,runtime_owner,incarnation_resources FROM agents_meta "
            "WHERE id=%s",
            (agent_id,),
        ).fetchone()
    if row is None:
        return None, None
    generation, owner, resources = row
    if generation is None or owner is None:
        return None, resources
    return RuntimeIncarnation(agent_id, generation, owner), resources


def main(argv: Sequence[str] | None = None) -> int:
    """List one agent's request evidence, or quarantine reviewed entries."""
    parser = argparse.ArgumentParser(
        prog="python -m shared.exec_request_evidence",
        description=(
            "List the exec request envelopes left under one agent's run/exec directory, "
            "or move reviewed envelopes into the explicit quarantine."
        ),
    )
    parser.add_argument("--agent", type=int, required=True, help="agent id to inspect")
    parser.add_argument(
        "--quarantine",
        action="append",
        default=[],
        metavar="FILE",
        help="quarantine one named request envelope (stale entries only, unless --force)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="quarantine a live or unattributable entry too, after manual review",
    )
    args = parser.parse_args(argv)
    incumbent, resources = _row_identity(args.agent)
    entries = survey(args.agent, incumbent=incumbent, resources=resources)
    if not args.quarantine:
        _report(args.agent, entries)
        return 0
    known = {entry.path.name: entry for entry in entries}
    selected: list[RequestEvidence] = []
    refused = False
    for name in args.quarantine:
        entry = known.get(Path(name).name)
        if entry is None:
            _emit(f"no request evidence named {name!r} under agent {args.agent}")
            refused = True
            continue
        if entry.retained and not args.force:
            _emit(f"refusing to quarantine {entry.describe()} (pass --force after review)")
            refused = True
            continue
        selected.append(entry)
    if refused:
        return 1
    if not selected:
        return 0
    report = _commit(args.agent, selected, reason="manual quarantine")
    for item in report.quarantined:
        _emit(f"quarantined {item.entry.path} -> {item.destination}")
    return 0


def _report(agent_id: int, entries: Sequence[RequestEvidence]) -> None:
    """The listing an operator reads before deciding anything else."""
    if not entries:
        _emit(f"agent {agent_id}: no exec request evidence")
        return
    retained = [entry for entry in entries if entry.retained]
    for entry in entries:
        _emit(f"{'retained' if entry.retained else 'would quarantine'}: {entry.describe()}")
    _emit(f"agent {agent_id}: {len(entries) - len(retained)} stale, {len(retained)} retained")
    for entry in retained:
        _emit(
            f"  {sys.executable} -m shared.exec_request_evidence --agent {agent_id} "
            f"--quarantine {entry.path.name} [--force]"
        )


def _emit(line: str) -> None:
    """One CLI output line on stdout (`print` is lint-banned in shared/)."""
    sys.stdout.write(line + "\n")


def _commit(agent_id: int, entries: Sequence[RequestEvidence], *, reason: str) -> QuarantineReport:
    """Move already-classified entries; shared by the automatic and manual paths."""
    event_dir = quarantined_exec_requests_dir() / f"{_slug(reason)}-{_stamp()}"
    destination_dir = _private_dir(event_dir / str(agent_id))
    moved: list[QuarantinedEvidence] = []
    retained: list[RequestEvidence] = []
    vanished: list[str] = []
    for entry in entries:
        destination = _free_name(destination_dir / entry.path.name)
        try:
            shutil.move(str(entry.path), str(destination))
        except FileNotFoundError:
            vanished.append(str(entry.path))
        except OSError as exc:
            retained.append(_replaced(entry, f"quarantine failed: {exc}"))
        else:
            moved.append(QuarantinedEvidence(entry, destination))
    if moved:
        _write_receipt(destination_dir / "receipt.json", agent_id, reason, moved)
    if moved or vanished:
        logger.info(
            "exec request evidence quarantined: {preserved} preserved, {settled} already settled",
            event="exec_request_quarantine",
            agent_id=agent_id,
            reason=reason,
            event_dir=str(event_dir),
            sources=[str(item.entry.path) for item in moved],
            vanished=vanished,
            preserved=len(moved),
            settled=len(vanished),
        )
    return QuarantineReport(agent_id, event_dir, tuple(moved), tuple(retained))


if __name__ == "__main__":
    raise SystemExit(main())

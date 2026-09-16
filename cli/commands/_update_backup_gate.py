"""Backup-pipeline gate for the update stop (task #3661, from #3591).

The 2026-09-16 wave abort (rc=5) came from the gateway's local stop meeting the
daily logical backup: its off-site publish runs inside the `pg-backup` scheduler
for the length of the upload, the 300-second stop window expired against it, and
the rollout aborted with the host half-stopped. The operator's manual dispatch
gate — release the stop only when PG backup/publish is idle — is replayed here,
so the stop refuses before anything is signalled.

Two determinations, one per half of that gate:

- the scheduler's own job state: `pg-backup`'s `/healthz` reports
  `progress: "running <n>s"` while a dump, its encryption, its off-site publish
  or the weekly restore drill executes. The dump runs inside the daemon, so a
  process scan cannot see this work; the progress field is the signal.
- a stand-alone off-site publish: `python -m services.backup --publish-offsite
  <artifact>` runs detached (the updater's own async upload, the operator retry
  units) and never appears in the daemon's progress.

A scheduler that does not answer is reported, not treated as busy: a disabled or
unhealthy daemon must not brick updates, and the watchdog restores it on its own.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import cast

import psutil

from services.backup import backup_dir
from shared.daemon_health import read_health_payload
from shared.exit_codes import RESTART_DECLINED_EXIT_CODE

_SCHEDULER_DAEMON = "pg_backup"
_SCHEDULER_COMPONENT = "backup"
_HEALTH_TIMEOUT_S = 2.0
_PUBLISH_MODULE = "services.backup"
_PUBLISH_FLAG = "--publish-offsite"
_DISABLE_HINT = "AVA_UPDATE_BACKUP_PRECHECK=false"


def _precheck_enabled() -> bool:
    """`AVA_UPDATE_BACKUP_PRECHECK` as a call-time read — the gate's off switch."""
    from shared.config import settings

    return settings.gateway.update_backup_precheck


def _backup_component(payload: dict[str, object]) -> dict[str, object] | None:
    """The scheduler's own `backup` record from one `/healthz` payload."""
    components = payload.get("components")
    if not isinstance(components, list):
        return None
    for record in cast("list[object]", components):
        if not isinstance(record, dict):
            continue
        candidate = cast("dict[str, object]", record)
        if candidate.get("name") == _SCHEDULER_COMPONENT:
            return candidate
    return None


def _scheduler_job_finding() -> tuple[str | None, str | None]:
    """(in-flight finding, unreadable note) from the `pg-backup` scheduler.

    `services.backup_scheduler.daemon` reports `progress: "running <n>s"` for its
    whole current job and `"idle"` between jobs — the field the operator's manual
    gate read by hand on 2026-09-16.
    """
    payload = read_health_payload(_SCHEDULER_DAEMON, timeout_s=_HEALTH_TIMEOUT_S)
    if payload is None:
        return None, (
            "the pg-backup scheduler did not answer its /healthz — a running job "
            "cannot be verified (a disabled or unhealthy daemon is not treated as busy)"
        )
    component = _backup_component(payload)
    progress = component.get("progress") if component is not None else None
    if not isinstance(progress, str):
        return None, (
            "the pg-backup scheduler /healthz carries no backup progress — a "
            "running job cannot be verified"
        )
    if progress.startswith("running"):
        return (
            f"logical backup job is in flight — the pg-backup scheduler reports "
            f"progress={progress!r} (healthz component 'backup'; the dump and its "
            f"off-site publish run inside the daemon)",
            None,
        )
    return None, None


def _publish_process_finding() -> str | None:
    """A live stand-alone off-site publish of this unit's managed dumps.

    Matched on the exact `-m services.backup --publish-offsite` token pair with
    the artifact living under THIS unit's `backups/db` — a co-located cluster's
    publish is not ours to wait on, and a zombie is not uploading anything.
    """
    directory = str(backup_dir()) + os.sep
    for process in psutil.process_iter(["pid", "cmdline", "status"]):
        info = process.info
        if info.get("status") == psutil.STATUS_ZOMBIE:
            continue
        cmdline = info.get("cmdline")
        if not cmdline or _PUBLISH_MODULE not in cmdline or _PUBLISH_FLAG not in cmdline:
            continue
        position = cmdline.index(_PUBLISH_FLAG) + 1
        artifact = cmdline[position : position + 1]
        if not artifact or not artifact[0].startswith(directory):
            continue
        return (
            f"off-site publish of {Path(artifact[0]).name} is still uploading "
            f"(pid {info.get('pid')}, detached `python -m services.backup --publish-offsite`)"
        )
    return None


def refuse_inflight_backup() -> int | None:
    """Print the stop pre-check verdict; return the refusal rc or None.

    The gateway update leg calls this immediately before its stop. A refusal
    means nothing was signalled: the leg returns the rc as-is (host still
    serving), and the orchestration's compensating resume restores the paused
    agent-runners.
    """
    if not _precheck_enabled():
        print(f"  · backup precheck: disabled ({_DISABLE_HINT})")
        return None
    findings: list[str] = []
    notes: list[str] = []
    scheduler, scheduler_note = _scheduler_job_finding()
    if scheduler is not None:
        findings.append(scheduler)
    if scheduler_note is not None:
        notes.append(scheduler_note)
    publish = _publish_process_finding()
    if publish is not None:
        findings.append(publish)
    if findings:
        print(
            "\n✗ refusing the update stop: this host's backup pipeline is in flight"
            " — nothing was stopped, and the gateway keeps serving.",
            file=sys.stderr,
        )
        for finding in findings:
            print(f"  · {finding}", file=sys.stderr)
        print(
            "  The 2026-09-16 wave abort (rc=5) was this stop meeting the daily dump's"
            " off-site publish. Re-run `ava cluster update` once the pipeline is idle,"
            f" or set {_DISABLE_HINT} to dispatch anyway.",
            file=sys.stderr,
        )
        return RESTART_DECLINED_EXIT_CODE
    print(
        "  · backup precheck: idle"
        if not notes
        else "  · backup precheck: no in-flight pipeline found"
    )
    for note in notes:
        print(f"  · note: {note}")
    return None

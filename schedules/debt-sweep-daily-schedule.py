"""Reconcile Ava's tech-debt ledger once per cluster day at 06:30.

Each claimed cron slot first runs the project-local mechanical debt scan and
persists its report, then wakes exactly one debt-clearing worker. The worker
uses the normal worktree and PR workflow; this schedule owns no resident
worker. Claims commit before this callback runs, so a process failure is an
intentional at-most-once loss that the P0 lead must recover manually.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Sequence, cast
from zoneinfo import ZoneInfo

import ava
import shared
from ava.agents import AgentStatus as S
from schedules.agent_status_guard import ensure_agent_status_members
from schedules.catchup import catch_up, claimed_slot, fire_slot_once
from shared.config import settings
from shared.log import init_gateway_process
from shared.paths import ava_home
from shared.daemon.schedules.watcher import next_fire

ensure_agent_status_members(
    S,
    {"IDLING", "RUNNING", "TERMINATED"},
    schedule_name="debt-sweep-daily",
)

CRON = "30 6 * * *"
TZ = settings.general.timezone
_REPO_ROOT = Path(shared.__file__).resolve().parents[1]
_LEDGER_PATH = "future/tech-debt/ledger.md"
_PROCESS_NAME = "schedule-debt-sweep-daily"
_WORKER_LABEL_ENV = "AVA_DEBT_SWEEP_AGENT_LABEL"
_DEFAULT_WORKER_LABEL = "debt-sweep-daily"
_REPORT_AGENT_ENV = "AVA_DEBT_SWEEP_REPORT_AGENT"
_REPORT_LABEL = "Ava \u8d1f\u8d23\u4eba"
_SCAN_TIMEOUT_SECONDS = 15 * 60


@dataclass(frozen=True)
class ScanReport:
    """The durable scan artifact and its status for one clearing pass."""

    succeeded: bool
    artifact_path: Path
    summary: str
    report: str = ""


@dataclass(frozen=True)
class WorkerDispatch:
    """One worker action, recorded in the day's telemetry event."""

    agent_id: int
    action: Literal["spawned", "resurrected", "messaged"]


def _worker_label() -> str:
    configured = os.environ.get(_WORKER_LABEL_ENV)
    return (
        configured.strip()
        if configured is not None and configured.strip()
        else _DEFAULT_WORKER_LABEL
    )


def _report_agent() -> int:
    configured = os.environ.get(_REPORT_AGENT_ENV)
    if configured is not None and configured.strip():
        try:
            agent_id = int(configured)
        except ValueError as exc:
            raise RuntimeError(f"{_REPORT_AGENT_ENV} must be a numeric agent id") from exc
        if agent_id <= 0:
            raise RuntimeError(f"{_REPORT_AGENT_ENV} must be a positive agent id")
        return agent_id

    before_id = None
    while True:
        page = ava.agents.list_agents(scope="all", query=_REPORT_LABEL, before_id=before_id)
        for agent in page.agents:
            if agent.label == _REPORT_LABEL and agent.status in (S.RUNNING, S.IDLING, S.TERMINATED):
                return agent.agent_id
        if page.next_cursor is None:
            raise RuntimeError(f"no report agent labelled {_REPORT_LABEL!r} is available")
        before_id = page.next_cursor


def _report_failure(detail: str) -> None:
    message = (
        "Daily debt sweep failed:\n"
        f"{detail[-1000:]}\n"
        "Check the schedule log and the saved mechanical-scan report, then run the "
        "debt-clearing pass manually with the sweeper skills."
    )
    try:
        ava.agents.send_message(_report_agent(), message)
    except Exception as exc:
        print(f"debt-sweep-daily could not notify the P0 lead: {exc}")
        raise


def ensure_worker(label: str, prompt: str) -> WorkerDispatch:
    """Reuse a matching worker by label, or spawn the day's one worker."""
    before_id = None
    while True:
        page = ava.agents.list_agents(scope="all", query=label, before_id=before_id)
        for agent in page.agents:
            if agent.label != label:
                continue
            if agent.status == S.TERMINATED:
                ava.agents.resurrect(agent.agent_id, prompt)
                return WorkerDispatch(agent_id=agent.agent_id, action="resurrected")
            ava.agents.send_message(agent.agent_id, prompt)
            return WorkerDispatch(agent_id=agent.agent_id, action="messaged")
        if page.next_cursor is None:
            break
        before_id = page.next_cursor
    return WorkerDispatch(
        agent_id=cast(int, ava.agents.spawn(prompt=prompt, label=label)),  # pyright: ignore[reportCallIssue] — fleet plugin wraps spawn with label
        action="spawned",
    )


def _scan_artifact_path(day: str, slot: datetime, *, ensure_home: bool) -> Path:
    slot_stamp = slot.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    root = ava_home() if ensure_home else Path(settings.general.ava_home).expanduser()
    return root / "debt-sweeps" / day / f"mechanical-scan-{slot_stamp}.txt"


def _scan_summary(*, succeeded: bool, report: str) -> str:
    lines = [line for line in report.splitlines() if line.strip()]
    outcome = "completed" if succeeded else "failed"
    final_line = lines[-1] if lines else "no output"
    return f"mechanical scan {outcome} ({len(lines)} lines; final: {final_line[:240]})"


def _run_mechanical_scan(repo: Path, artifact_path: Path | None) -> ScanReport:
    script = repo / ".agents" / "skills" / "ava-sweeper" / "run.sh"
    try:
        completed = subprocess.run(
            ["bash", str(script), "--repo", str(repo)],
            check=False,
            capture_output=True,
            text=True,
            timeout=_SCAN_TIMEOUT_SECONDS,
        )
        report = completed.stdout + completed.stderr
        succeeded = completed.returncode == 0
        if not succeeded:
            report += f"\nmechanical scan exited {completed.returncode}\n"
    except (OSError, subprocess.TimeoutExpired) as exc:
        report = f"mechanical scan could not run: {type(exc).__name__}: {exc}\n"
        succeeded = False

    resolved_artifact = artifact_path or Path("<dry-run mechanical-scan artifact>")
    if artifact_path is not None:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(report, encoding="utf-8")
    return ScanReport(
        succeeded=succeeded,
        artifact_path=resolved_artifact,
        summary=_scan_summary(succeeded=succeeded, report=report),
        report=report,
    )


def worker_prompt(day: str, scan: ScanReport) -> str:
    """The complete, one-run instruction for the debt-clearing worker."""
    return f"""You are today's debt-clearing pass for the Ava repository ({day}).

Start a fresh worktree from origin/main and follow the normal PR workflow; never push main.
Read and follow the `ava.skills.sweeper` engine and `.agents/skills/ava-sweeper/` project skill.
Reconcile the single ledger at `{_LEDGER_PATH}`.

The mechanical scan artifact is `{scan.artifact_path}`.
Scan summary: {scan.summary}
Use the artifact as evidence, re-check findings as needed, and keep the full mechanical lists in
the PR body rather than the ledger. Record only strong, fingerprinted findings; respect wontfix
entries and remove resolved entries.

Deliver at most ONE `chore(sweeper): debt reconcile {day}` PR. If reconciliation changes nothing,
open no PR and report the no-op. When done, report your result and end your own process.
"""


def event_payload(*, day: str, scan: ScanReport, dispatch: WorkerDispatch) -> dict[str, str | int]:
    """The compact fact emitted once for a claimed debt-clearing slot."""
    return {
        "day": day,
        "scan_status": "ok" if scan.succeeded else "failed",
        "action": dispatch.action,
        "worker_agent_id": dispatch.agent_id,
    }


def _slot_day(slot: datetime) -> str:
    return slot.astimezone(ZoneInfo(TZ)).strftime("%Y-%m-%d")


def _fire(_payload: None) -> None:
    slot = claimed_slot()
    if slot is None:
        detail = "RuntimeError: debt-sweep-daily fired outside a claimed slot"
        print(f"debt-sweep-daily failed: {detail}")
        _report_failure(detail)
        return
    try:
        day = _slot_day(slot)
        scan = _run_mechanical_scan(
            _REPO_ROOT,
            _scan_artifact_path(day, slot, ensure_home=True),
        )
        dispatch = ensure_worker(_worker_label(), worker_prompt(day, scan))
        init_gateway_process(name=_PROCESS_NAME)
        from shared import telemetry

        telemetry.emit(
            "telemetry",
            "debt_sweep_daily",
            source="system",
            attributes=event_payload(day=day, scan=scan, dispatch=dispatch),
        )
        print(
            f"[{datetime.now(UTC).isoformat()}] debt-sweep-daily: {day} — "
            f"scan={'ok' if scan.succeeded else 'failed'}, {dispatch.action} worker {dispatch.agent_id}"
        )
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        print(f"debt-sweep-daily failed: {detail}")
        _report_failure(detail)


def _dry_run(repo: Path) -> None:
    slot = datetime.now(UTC)
    day = _slot_day(slot)
    artifact_path = _scan_artifact_path(day, slot, ensure_home=False)
    scan = _run_mechanical_scan(repo, None)
    scan = ScanReport(
        succeeded=scan.succeeded,
        artifact_path=artifact_path,
        summary=scan.summary,
        report=scan.report,
    )
    label = _worker_label()
    preview_dispatch = WorkerDispatch(agent_id=0, action="spawned")

    print(f"dry-run: mechanical scan report would be written to: {scan.artifact_path}")
    print(f"dry-run: mechanical scan summary: {scan.summary}")
    print("dry-run: mechanical scan report:")
    print(scan.report, end="" if scan.report.endswith("\n") else "\n")
    print(f"dry-run: resolved worker label: {label}")
    print("dry-run: prompt preview:")
    print(worker_prompt(day, scan), end="")
    print("dry-run: event payload:")
    print(json.dumps(event_payload(day=day, scan=scan, dispatch=preview_dispatch), sort_keys=True))
    print("dry-run: no claims, agent operations, telemetry, or database access were performed.")


def _main_loop() -> None:
    catch_up([(CRON, None)], timezone=TZ, fire=_fire)
    last_run_at = datetime.now(UTC)
    while True:
        now = datetime.now(UTC)
        next_run = next_fire(CRON, after=now - timedelta(minutes=2), timezone=TZ)
        if next_run <= last_run_at:
            next_run = next_fire(CRON, after=last_run_at, timezone=TZ)
        wait_seconds = (next_run - now).total_seconds()
        if wait_seconds > 0:
            time.sleep(min(wait_seconds, 3600))
            continue
        fire_slot_once(next_run, None, fire=_fire)
        last_run_at = datetime.now(UTC)
        time.sleep(120)


def build_parser() -> argparse.ArgumentParser:
    """The script's CLI — flags for manual runs and dry-run demos."""
    parser = argparse.ArgumentParser(description="Run the daily tech-debt clearing schedule.")
    parser.add_argument("--once", action="store_true", help="run one dry-run demonstration")
    parser.add_argument("--dry-run", action="store_true", help="scan without schedule side effects")
    parser.add_argument("--repo", type=Path, help="repository root for --once --dry-run")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.dry_run and not args.once:
        parser.error("--dry-run requires --once")
    if args.once:
        if not args.dry_run:
            parser.error("--once requires --dry-run")
        _dry_run((args.repo or _REPO_ROOT).resolve())
        return
    _main_loop()


if __name__ == "__main__":
    main()

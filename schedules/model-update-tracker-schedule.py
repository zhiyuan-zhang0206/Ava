"""Daily official-model detection, reporting actionable additions to the P0 lead."""

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import ava
from ava.agents import AgentStatus as S
from schedules.agent_status_guard import ensure_agent_status_members
from schedules.catchup import catch_up, fire_slot_once
from shared.config import settings
from shared.paths import ava_home, repo_root
from shared.watcher import next_fire

ensure_agent_status_members(
    S,
    {"IDLING", "RUNNING", "TERMINATED"},
    schedule_name="model-update-tracker",
)

CRON = "0 6 * * *"
# The cluster default is Asia/Shanghai. Keeping this config-derived matches the
# other built-ins and preserves one cluster wall clock when operators change it.
TZ = settings.general.timezone
_REPORT_LABEL = "Ava \u8d1f\u8d23\u4eba"
_REPORT_AGENT_ENV = "AVA_MODEL_UPDATE_REPORT_AGENT"
_TIMEOUT_SECONDS = 120


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
    matches = [
        agent
        for agent in ava.agents.list_agents(filter_by_status=(S.RUNNING, S.IDLING, S.TERMINATED))
        if agent.label == _REPORT_LABEL
    ]
    if not matches:
        raise RuntimeError(f"no report agent labelled {_REPORT_LABEL!r} is available")
    return max(matches, key=lambda agent: agent.agent_id).agent_id


def _send_report(message: str) -> None:
    try:
        ava.agents.send_message(_report_agent(), message)
    except Exception as exc:
        print(f"model update tracker could not notify the P0 lead: {exc}")
        raise


def _failure_message(detail: str) -> str:
    return f"Model update tracker failed:\n{detail[-1000:]}\nCheck the schedule log and report directory."


def _candidate_message(payload: dict[str, Any], report_dir: Path) -> str:
    actionable = payload["actionable_candidates"]
    if not isinstance(actionable, dict):
        raise TypeError("report actionable_candidates must be an object")
    providers = payload["providers"]
    if not isinstance(providers, dict):
        raise TypeError("report providers must be an object")
    lines = ["New upstream model candidates:"]
    for provider, model_ids in actionable.items():
        if not isinstance(provider, str) or not isinstance(model_ids, list):
            raise TypeError("report actionable candidate has an invalid shape")
        provider_report = providers[provider]
        if not isinstance(provider_report, dict):
            raise TypeError("report provider result must be an object")
        series_models = provider_report["series_models"]
        if not isinstance(series_models, dict):
            raise TypeError("report series_models must be an object")
        for model_id in model_ids:
            if not isinstance(model_id, str):
                raise TypeError("report candidate id must be a string")
            line = f"- {provider}: {model_id}"
            known = series_models[model_id]
            if not isinstance(known, list) or not all(isinstance(value, str) for value in known):
                raise ValueError("report same-series ids must be strings")
            if known:
                line += f" (registry already binds {', '.join(known)} in this series; review supersession)"
            lines.append(line)
    status_changes = payload["status_changes"]
    if not isinstance(status_changes, list) or not all(
        isinstance(value, str) for value in status_changes
    ):
        raise ValueError("report status_changes must be a list of strings")
    if status_changes:
        lines.append(f"Provider status changed: {', '.join(status_changes)}")
    lines.append(f"Full report: {report_dir / 'last-report.md'}")
    return "\n".join(lines)


def _report_from_result(result: subprocess.CompletedProcess[str], report_dir: Path) -> None:
    if result.returncode == 0:
        print(f"[{datetime.now(UTC).isoformat()}] model update tracker: no new candidates")
        return
    if result.returncode == 2:
        payload = json.loads((report_dir / "last-report.json").read_text())
        if not isinstance(payload, dict):
            raise ValueError("model update tracker JSON report must be an object")
        _send_report(_candidate_message(payload, report_dir))
        return
    detail = (result.stdout + result.stderr)[-1000:]
    _send_report(_failure_message(f"exit code {result.returncode}\n{detail}"))


def run_tracker() -> None:
    """Run one detection cycle and notify on candidate, error, or status change."""
    root = repo_root()
    report_dir = ava_home() / "model-tracker"
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = str(root)
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(root / "scripts" / "check_model_updates.py"),
                "--write-report",
                str(report_dir),
            ],
            cwd=root,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
        _report_from_result(result, report_dir)
    except Exception as exc:
        _send_report(_failure_message(f"subprocess exception: {exc}"))


def _fire_tracker(_trigger: None) -> None:
    run_tracker()


def _main_loop() -> None:
    catch_up([(CRON, None)], timezone=TZ, fire=_fire_tracker)
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
        fire_slot_once(next_run, None, fire=_fire_tracker)
        last_run_at = datetime.now(UTC)
        time.sleep(120)


if __name__ == "__main__":
    _main_loop()

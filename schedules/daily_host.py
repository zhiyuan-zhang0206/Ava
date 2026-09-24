"""Shared slot loop and report recipient lookup for the two daily CI hosts."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import ava
from ava.agents import AgentStatus as S
from schedules.catchup import catch_up, fire_slot_once
from shared.watcher import next_fire


def report_agent(env_name: str, label: str) -> int:
    """Resolve an explicit recipient or search every page for the exact label."""
    configured = os.environ.get(env_name)
    if configured is not None and configured.strip():
        try:
            agent_id = int(configured)
        except ValueError as exc:
            raise RuntimeError(f"{env_name} must be a numeric agent id") from exc
        if agent_id <= 0:
            raise RuntimeError(f"{env_name} must be a positive agent id")
        return agent_id

    before_id = None
    while True:
        page = ava.agents.list_agents(scope="all", query=label, before_id=before_id)
        for agent in page.agents:
            if agent.label == label and agent.status in (S.RUNNING, S.IDLING, S.TERMINATED):
                return agent.agent_id
        if page.next_cursor is None:
            raise RuntimeError(f"no report agent labelled {label!r} is available")
        before_id = page.next_cursor


def run_daily_loop(cron: str, tz: str, fire: Callable[[None], None]) -> None:
    """Catch up bounded missed slots, then claim each due slot at most once."""
    catch_up([(cron, None)], timezone=tz, fire=fire)
    last_run_at = datetime.now(UTC)
    while True:
        now = datetime.now(UTC)
        next_run = next_fire(cron, after=now - timedelta(minutes=2), timezone=tz)
        if next_run <= last_run_at:
            next_run = next_fire(cron, after=last_run_at, timezone=tz)
        wait_seconds = (next_run - now).total_seconds()
        if wait_seconds > 0:
            time.sleep(min(wait_seconds, 3600))
            continue
        fire_slot_once(next_run, None, fire=fire)
        last_run_at = datetime.now(UTC)
        time.sleep(120)

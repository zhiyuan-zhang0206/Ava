"""self-evolution-weekly — dynamic trigger based on weekly event volume.

Fires the self-evolution agent on Tuesday 00:00 cluster time, but only when the
past week's agent event volume exceeds a minimum threshold. Busy weeks also get
a Thursday mid-week follow-up trigger.

Resumable: recomputes on every iteration, acts only when the window is open.
"""

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import ava
from ava.agents import AgentStatus as S
from schedules.agent_status_guard import ensure_agent_status_members
from schedules.catchup import catch_up, fire_slot_once
from shared.config import settings
from shared.watcher import next_fire

ensure_agent_status_members(
    S,
    {"IDLING", "RESTARTING", "RUNNING", "TERMINATED"},
    schedule_name="self-evolution-weekly",
)

# ── Configuration ──────────────────────────────────────────────────────────

LABEL = "self-evolution"
PROMPT = "Read and run $AVA_HOME/skills/ava-self-evolution/SKILL.md for this week."
# Cluster wall clock (`AVA_TIMEZONE`, cluster-pinned), never the host's OS
# timezone — a weekly cron is the case where a host-local reading lands the run
# on the wrong CALENDAR DAY, not merely at the wrong hour. Read at process
# start; `ava schedules restart <id>` adopts a changed AVA_TIMEZONE.
TIMEZONE = settings.general.timezone

# Main weekly check: Tuesday 00:00 cluster time
MONDAY_CRON = "0 0 * * 2"
# Mid-week follow-up for busy weeks: Friday 00:00 cluster time
THURSDAY_CRON = "0 0 * * 5"
MONDAY_TRIGGER = "monday"
THURSDAY_TRIGGER = "thursday"

# Minimum weekly events to trigger self-evolution (~4K/day floor).
# Below this the week is too quiet for meaningful analysis.
MIN_WEEKLY_EVENTS = 30000

# Above this threshold, also schedule a Thursday mid-week follow-up.
HIGH_WEEKLY_EVENTS = 200000

# ── Helpers ────────────────────────────────────────────────────────────────


def count_events(since: datetime) -> int:
    """Count events since `since` (UTC) via the Loki-backed /api/events count
    path. PG `events` is a frozen archive since 2026-08-12 (Task #1197 LGTM
    cutover) — the weekly trigger must count the live stream or it silently
    skips every week (2026-08-14 missed-consumer audit)."""
    import os

    import httpx

    from shared.paths import ava_home

    env_file = Path(ava_home()) / ".env"
    secret = ""
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("AVA_CLUSTER_SECRET="):
                secret = line.split("=", 1)[1].strip().strip("\"'")
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    base = os.environ.get("AVA_GATEWAY_URL", "http://localhost:8000")
    params = {
        "from": since.isoformat(),
        "to": datetime.now(UTC).isoformat(),
        "limit": 1,
        "offset": 0,
        # 2026-08-18 contract change: meta.total is opt-in; without this flag
        # the gateway returns total=None and int(None) crashes the weekly
        # trigger (and a silent 0 would skip the week's deep run).
        "with_total": 1,
    }
    resp = httpx.get(f"{base}/api/events", params=params, headers=headers, timeout=120.0)
    resp.raise_for_status()
    payload = resp.json()
    total = payload.get("meta", {}).get("total")
    if total is None:
        raise RuntimeError(
            f"/api/events returned no total despite with_total=1: {str(payload)[:300]}"
        )
    return int(total)


def ensure_agent(label: str, prompt: str) -> int:
    """Return the agent id of a running/idle agent with `label`, resurrecting a
    terminated one or spawning a fresh one if none exists."""
    mine = [
        a
        for a in ava.agents.list_agents(
            filter_by_status=(
                S.RUNNING,
                S.IDLING,
                S.TERMINATED,
                S.RESTARTING,
            )
        )
        if a.label == label
    ]
    if mine:
        a = max(mine, key=lambda r: r.agent_id)
        if a.status == S.TERMINATED:
            ava.agents.resurrect(a.agent_id, prompt)
        else:
            ava.agents.send_message(a.agent_id, prompt)
        return a.agent_id
    return ava.agents.spawn(prompt=prompt, label=label)  # pyright: ignore[reportCallIssue] — fleet plugin wraps spawn with label


def fire(prompt: str) -> None:
    """Trigger the self-evolution agent."""
    agent_id = ensure_agent(LABEL, prompt)
    print(f"[{datetime.now(UTC).isoformat()}] self-evolution agent #{agent_id} triggered")


def should_fire_monday() -> bool:
    """Check whether Monday trigger should fire based on event volume."""
    week_ago = datetime.now(UTC) - timedelta(days=7)
    total = count_events(week_ago)
    print(
        f"[{datetime.now(UTC).isoformat()}] "
        f"self-evolution check: {total} events in past 7 days "
        f"(min={MIN_WEEKLY_EVENTS}, high={HIGH_WEEKLY_EVENTS})"
    )
    if total < MIN_WEEKLY_EVENTS:
        print(
            f"[{datetime.now(UTC).isoformat()}] "
            f"self-evolution skipped: {total} < {MIN_WEEKLY_EVENTS} minimum"
        )
        return False
    return True


def should_fire_thursday() -> bool:
    """Check whether the mid-week follow-up (Friday 00:00 cluster time) should fire."""
    monday = datetime.now(UTC) - timedelta(days=4)
    count = count_events(monday)
    bar = HIGH_WEEKLY_EVENTS // 2
    print(
        f"[{datetime.now(UTC).isoformat()}] "
        f"self-evolution Thursday check: {count} events since Monday "
        f"(bar={bar})"
    )
    return count >= bar


# ── Main loop ──────────────────────────────────────────────────────────────


def _main_loop() -> None:
    """The gateway runs this file as `python self-evolution-weekly-schedule.py`
    (never imports it); the guard keeps the loop out of import, so tests can
    load the module and call count_events directly."""
    thursday_enabled = False

    def fire_weekly_trigger(trigger: str) -> None:
        nonlocal thursday_enabled
        if trigger == MONDAY_TRIGGER:
            if should_fire_monday():
                fire(PROMPT)
                week_ago = datetime.now(UTC) - timedelta(days=7)
                total = count_events(week_ago)
                thursday_enabled = total >= HIGH_WEEKLY_EVENTS
                if thursday_enabled:
                    print(
                        f"[{datetime.now(UTC).isoformat()}] self-evolution: "
                        f"Thursday follow-up enabled ({total} >= {HIGH_WEEKLY_EVENTS})"
                    )
            else:
                thursday_enabled = False
            return
        if trigger == THURSDAY_TRIGGER:
            if should_fire_thursday():
                fire(f"{PROMPT} (mid-week follow-up)")
            thursday_enabled = False
            return
        raise ValueError(f"unknown self-evolution weekly trigger: {trigger}")

    catch_up(
        [
            (MONDAY_CRON, MONDAY_TRIGGER),
            (THURSDAY_CRON, THURSDAY_TRIGGER),
        ],
        timezone=TIMEZONE,
        fire=fire_weekly_trigger,
    )

    while True:
        now = datetime.now(UTC)

        # after=now-2min gives trigger tolerance: sleep precision delay can land `now`
        # a fraction of a second past the hour; croniter get_next (strictly > base)
        # would then jump to the next day (deterministic miss, observed 2026-08-06).
        # Tolerance window = [-120s, +90s].
        nxt_monday = next_fire(MONDAY_CRON, after=now - timedelta(minutes=2), timezone=TIMEZONE)
        wait_monday = (nxt_monday - now).total_seconds()

        nxt_thursday = next_fire(THURSDAY_CRON, after=now - timedelta(minutes=2), timezone=TIMEZONE)
        wait_thursday = (nxt_thursday - now).total_seconds() if thursday_enabled else float("inf")

        wait = min(wait_monday, wait_thursday)

        if wait > 90:
            time.sleep(min(wait, 3600))
            continue

        if wait_monday <= 90:
            fire_slot_once(
                nxt_monday,
                MONDAY_TRIGGER,
                fire=fire_weekly_trigger,
            )
            time.sleep(120)
            continue

        if wait_thursday <= 90:
            fire_slot_once(
                nxt_thursday,
                THURSDAY_TRIGGER,
                fire=fire_weekly_trigger,
            )
            time.sleep(120)
            continue


if __name__ == "__main__":
    _main_loop()

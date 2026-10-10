"""self-evolution daily incremental scan — daily 00:00 cluster time.

Runs daily_scan.py (collect --days 1 + metrics + threshold alerts).
- exit code 2 (ALERT) -> wakes the self-evolution agent to act
- timeout/failure/missing script -> prints to the schedule log (runner
  records last_error) AND wakes the agent to investigate
Resumable: recomputes next_fire from the clock every iteration.
"""

from uuid import uuid4

import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import ava
from ava.agents import AgentStatus as S
from schedules.agent_status_guard import ensure_agent_status_members
from schedules.catchup import catch_up, cluster_timezone, fire_slot_once
from base.daemon.schedules.watcher import next_fire
from base.daemon.schedules.inputs import ScheduleInputs
from schedules.entry import schedule_entry


# daily_scan.py ships with the ava-self-evolution skill. The load-dir copy is
# converge-managed but bootstrap-only (R5): converge lands it once, and the
# product rollout's update legs refresh it to the landed revision (issue
# #1289 — before that wiring, the copy stayed at its first-landing version
# and a script added later never arrived; `ava skill update` is the manual
# equivalent).
def _daily_scan_path() -> str:
    return os.path.join(
        os.environ.get("AVA_HOME", os.path.expanduser("~/.ava")),
        "skills",
        "ava-self-evolution",
        "reference",
        "daily_scan.py",
    )


CRON = "0 0 * * *"  # 00:00 cluster time — the off-peak trough of a cluster workday


# Daily report recipient (an agent id) — defaults to the CEO #228 per the
# 2026-08-09 ruling (daily reports replaced the weekly ones); an env override
# wins, and an explicit empty env value skips the report.
def _report_agent() -> str:
    return os.environ.get("AVA_SELF_EVOLUTION_DAILY_REPORT_AGENT", "228")


# Measured full-day scan T >= 108 min: 112.9 min total on 2026-09-25 while
# two collect streams ran (task #4743 log, #4750 tracker). 10800 s = 180 min,
# about 1.6x the measured contended T. This remains the final bound between
# a slow scan (allowed) and a wedged one: beyond it, subprocess.TimeoutExpired
# prints the timeout line and wakes the agent. The longer bound only delays
# wedge detection, which is accepted.
_SCAN_TIMEOUT_SECONDS = 10800


def ensure_agent(label: str, prompt: str) -> int:
    before_id = None
    while True:
        page = ava.agents.list_agents(scope="all", query=label, before_id=before_id)
        # Directory pages are newest first; substring search still needs an exact label match.
        for agent in page.agents:
            if agent.label != label:
                continue
            if agent.status == S.TERMINATED:
                ava.agents.resurrect(agent.agent_id, prompt)
            else:
                ava.agents.send_message(agent.agent_id, prompt)
            return agent.agent_id
        if page.next_cursor is None:
            break
        before_id = page.next_cursor
    return ava.agents.spawn(prompt=prompt, label=label, idempotency_key=str(uuid4()))  # pyright: ignore[reportCallIssue] — fleet plugin wraps spawn with label


def run_scan() -> None:
    daily = _daily_scan_path()
    if not os.path.isfile(daily):
        # A missing script must not masquerade as an ALERT: python exits 2
        # when it cannot open the file, which the rc==2 branch would read
        # as "bad runs found". Fail loudly and wake the agent instead.
        msg = (
            f"daily_scan.py missing at {daily} — run `ava skill update ava-self-evolution` "
            f"(or `ava skill update` for all repo-native skills) to refresh the load-dir copy"
        )
        print(f"[{datetime.now(UTC).isoformat()}] {msg}")
        ensure_agent("self-evolution", f"Daily scan cannot run: {msg}")
        return
    try:
        r = subprocess.run(
            [sys.executable, daily, "--days", "1"],
            timeout=_SCAN_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            check=False,
        )
        tail = (r.stdout or "")[-2000:]
        err = (r.stderr or "")[-500:]
        print(f"[{datetime.now(UTC).isoformat()}] scan rc={r.returncode}")
        print(tail)
        if r.returncode == 2:
            ensure_agent(
                "self-evolution",
                f"Daily scan ALERT ({datetime.now(UTC).isoformat()}):\n{tail}\n{err}\nReview the daily report and act.",
            )
        elif r.returncode != 0:
            print(f"scan failed rc={r.returncode}: {err}")
            ensure_agent(
                "self-evolution",
                f"Daily scan failed rc={r.returncode}:\n{err}\nCheck the schedule log and daily_scan.py.",
            )
    except subprocess.TimeoutExpired:
        print(f"scan timed out after {_SCAN_TIMEOUT_SECONDS}s")
        ensure_agent(
            "self-evolution",
            f"Daily scan timed out after {_SCAN_TIMEOUT_SECONDS}s — check whether daily_scan.py is stuck.",
        )
        return
    report_agent = _report_agent()
    if report_agent:
        try:
            ava.agents.send_message(
                int(report_agent),
                f"[self-evolution daily {datetime.now(UTC).strftime('%m-%d')}]\n{tail}",
            )
        except Exception as e:
            print(f"daily report to {report_agent} failed: {e}")


def _fire_scan(_slot: datetime, _trigger: None) -> None:
    run_scan()


def main(*, inputs: ScheduleInputs) -> None:
    db = inputs.database()
    catch_up(db, [(CRON, None)], timezone=cluster_timezone(), fire=_fire_scan)
    while True:
        # after=now-2min gives trigger tolerance: sleep precision delay can land `now`
        # a fraction of a second past the hour; croniter get_next (strictly > base)
        # would then jump to the next day (deterministic miss, observed 2026-08-06).
        # tolerance window = [-120s, +60s].
        now = datetime.now(UTC)
        nxt = next_fire(CRON, after=now - timedelta(minutes=2), timezone=cluster_timezone())
        wait = (nxt - now).total_seconds()
        if wait > 60:
            time.sleep(min(wait, 3600))
            continue
        fire_slot_once(db, nxt, None, fire=_fire_scan)
        time.sleep(120)


if __name__ == "__main__":
    ensure_agent_status_members(
        S,
        {"TERMINATED"},
        schedule_name="self-evolution-daily",
    )
    with schedule_entry(globals().get("AVA_SCHEDULE_INPUTS")) as inputs:
        main(inputs=inputs)

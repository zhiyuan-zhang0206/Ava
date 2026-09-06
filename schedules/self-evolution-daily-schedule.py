"""self-evolution daily incremental scan — daily 00:00 cluster time.

Runs daily_scan.py (collect --days 1 + metrics + threshold alerts).
- exit code 2 (ALERT) -> wakes the self-evolution agent to act
- timeout/failure/missing script -> prints to the schedule log (runner
  records last_error) AND wakes the agent to investigate
Resumable: recomputes next_fire from the clock every iteration.
"""

import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import ava
from ava.agents import AgentStatus as S
from schedules.agent_status_guard import ensure_agent_status_members
from schedules.catchup import catch_up, fire_slot_once
from shared.config import settings
from shared.watcher import next_fire

ensure_agent_status_members(
    S,
    {"IDLING", "RESTARTING", "RUNNING", "TERMINATED"},
    schedule_name="self-evolution-daily",
)

# daily_scan.py ships with the ava-self-evolution skill. The load-dir copy is
# converge-managed but bootstrap-only (R5): converge lands it once, and the
# product rollout's update legs refresh it to the landed revision (issue
# #1289 — before that wiring, the copy stayed at its first-landing version
# and a script added later never arrived; `ava skill update` is the manual
# equivalent).
DAILY = os.path.join(
    os.environ.get("AVA_HOME", os.path.expanduser("~/.ava")),
    "skills",
    "ava-self-evolution",
    "reference",
    "daily_scan.py",
)
CRON = "0 0 * * *"  # 00:00 cluster time — the off-peak trough of a cluster workday
# Cluster wall clock (`AVA_TIMEZONE`, cluster-pinned), never the host's OS
# timezone: the whole fleet fires at one instant regardless of where each
# machine sits. Read at process start — `ava schedules restart <id>` to adopt a
# changed AVA_TIMEZONE.
TZ = settings.general.timezone
# Daily report recipient (an agent id) — defaults to the CEO #228 per the
# 2026-08-09 ruling (daily reports replaced the weekly ones); an env override
# wins, and an explicit empty env value skips the report.
REPORT_AGENT = os.environ.get("AVA_SELF_EVOLUTION_DAILY_REPORT_AGENT", "228")


def ensure_agent(label: str, prompt: str) -> int:
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


def run_scan() -> None:
    if not os.path.isfile(DAILY):
        # A missing script must not masquerade as an ALERT: python exits 2
        # when it cannot open the file, which the rc==2 branch would read
        # as "bad runs found". Fail loudly and wake the agent instead.
        msg = (
            f"daily_scan.py missing at {DAILY} — run `ava skill update ava-self-evolution` "
            f"(or `ava skill update` for all repo-native skills) to refresh the load-dir copy"
        )
        print(f"[{datetime.now(UTC).isoformat()}] {msg}")
        ensure_agent("self-evolution", f"Daily scan cannot run: {msg}")
        return
    try:
        r = subprocess.run(
            [sys.executable, DAILY, "--days", "1"],
            timeout=1800,
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
        print("scan timed out after 1800s")
        ensure_agent(
            "self-evolution",
            "Daily scan timed out after 1800s — check whether daily_scan.py is stuck.",
        )
        return
    if REPORT_AGENT:
        try:
            ava.agents.send_message(
                int(REPORT_AGENT),
                f"[self-evolution daily {datetime.now(UTC).strftime('%m-%d')}]\n{tail}",
            )
        except Exception as e:
            print(f"daily report to {REPORT_AGENT} failed: {e}")


def _fire_scan(_trigger: None) -> None:
    run_scan()


def main() -> None:
    catch_up([(CRON, None)], timezone=TZ, fire=_fire_scan)
    while True:
        # after=now-2min gives trigger tolerance: sleep precision delay can land `now`
        # a fraction of a second past the hour; croniter get_next (strictly > base)
        # would then jump to the next day (deterministic miss, observed 2026-08-06).
        # tolerance window = [-120s, +60s].
        now = datetime.now(UTC)
        nxt = next_fire(CRON, after=now - timedelta(minutes=2), timezone=TZ)
        wait = (nxt - now).total_seconds()
        if wait > 60:
            time.sleep(min(wait, 3600))
            continue
        fire_slot_once(nxt, None, fire=_fire_scan)
        time.sleep(120)


if __name__ == "__main__":
    main()

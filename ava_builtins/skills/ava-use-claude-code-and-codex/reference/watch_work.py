"""Reference watcher body: wake when the coding agent's work file calls for action.

Launch this in the background with `ava.watcher.launch(code=..., timeout=..., name="...")` after
substituting WORK_FILE. Take that path from the `work_file=` line the spawn script
printed -- do not rebuild it from a filename convention, since the work file is
relocatable via `--work-file`. It polls that file's `STATUS:` line and only calls
`ava.agents.send_message(ava.self.AGENT_ID, ...)` when there is
something for you to do -- so you supervise a long coding session without watching
its screen and without burning turns polling.

Runtime notes:
- This program runs under the same Python environment, configuration, and machine
  credentials as the agent that launched it.
- It reads the work file directly; it does NOT look at the coding agent's screen
  (a background watcher cannot resolve your sessions). When it reports a possible
  stall, you -- the launching agent -- capture the screen yourself, since you hold
  the session id, to see whether the agent crashed or is waiting on something.
- `ava.agents.send_message(ava.self.AGENT_ID, content)` delivers `content` back to you (the launching agent) as
  a new incoming message.
"""

import re
import time
from pathlib import Path

import ava

# Substitute before launching: the `work_file=` path the spawn script printed.
WORK_FILE = "/path/to/work.md"

# How often to re-check the file.
POLL_SECONDS = 60

# How long STATUS may sit at WORKING with no change to the file before it counts
# as a possible stall worth a screen check.  Claude Code (Opus 4.8 / Fable 5) with xhigh
# effort may work 10-15 minutes without touching the work file; raise this to 900 for
# those runs to avoid false stall alerts.
STALL_SECONDS = 600

# Heartbeat: wake the agent after this many seconds even if the file is still
# being updated -- catches the case where the coding agent keeps writing to
# the work file but never changes STATUS away from WORKING, or forgets to write a
# STATUS line entirely.  Set shorter than STALL_SECONDS so periodic check-ins
# fire before the stall alarm.
HEARTBEAT_SECONDS = 480

# Hard limit: wake the agent after this many seconds no matter what --
# even if STATUS=WORKING and the file is still being updated.  Prevents
# a watcher bug from causing infinite silent waiting.  Set longer than
# STALL_SECONDS (this is the last-resort safety net, not the first alarm).
HARD_LIMIT_SECONDS = 7200

# The agent overwrites a single `STATUS: <TOKEN>` line every turn. These three
# need you; WORKING means "still going, leave it alone".
ACTIONABLE = ("DONE", "NEED_INPUT", "HANDOFF")
_STATUS = re.compile(r"^STATUS:\s*(\w+)", re.MULTILINE)


def read_status(path: str) -> str | None:
    """Return the current STATUS token in the file (first match), or None if it is not there yet."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        ava.agents.send_message(
            ava.self.AGENT_ID,
            f"work file deleted: {path} -- the coding agent may have removed its workspace",
        )
        return None
    matches = _STATUS.findall(text)
    return matches[0] if matches else None


def watch(path: str) -> None:
    """Poll `path` until its STATUS needs you, a heartbeat fires, or it stalls;
    remind once, return."""
    start_time = time.monotonic()
    last_change = time.monotonic()
    last_wake = time.monotonic()
    last_mtime: float | None = None
    while True:
        try:
            mtime: float | None = Path(path).stat().st_mtime
        except FileNotFoundError:
            mtime = None
        if mtime != last_mtime:
            last_mtime = mtime
            last_change = time.monotonic()

        status = read_status(path)
        if status is None and not Path(path).exists() and last_mtime is not None:
            # read_status already reminded; stop polling so we do not spam
            return
        elapsed_total = time.monotonic() - start_time
        elapsed_change = time.monotonic() - last_change
        elapsed_wake = time.monotonic() - last_wake

        # Hard limit: wake no matter what after HARD_LIMIT_SECONDS.
        if elapsed_total > HARD_LIMIT_SECONDS:
            ava.agents.send_message(
                ava.self.AGENT_ID,
                f"hard limit reached: watcher has been polling {path} for over "
                f"{HARD_LIMIT_SECONDS}s -- capture the coding agent's screen to check progress",
            )
            return

        # DONE / NEED_INPUT / HANDOFF → wake immediately.
        if status in ACTIONABLE:
            ava.agents.send_message(
                ava.self.AGENT_ID,
                f"coding agent reported STATUS: {status} in {path} -- "
                "read that file and decide the next step",
            )
            return

        # WORKING (or missing STATUS — the agent may have forgotten the line).
        if status in (None, "WORKING"):
            # Stall: file hasn't changed at all for STALL_SECONDS → likely stuck.
            if elapsed_change > STALL_SECONDS:
                ava.agents.send_message(
                    ava.self.AGENT_ID,
                    f"coding agent has been WORKING with no change to {path} for over "
                    f"{STALL_SECONDS}s -- capture its screen to check for a stall",
                )
                return

            # Heartbeat: too long since last wake, even if the file is changing.
            # Catches the case where the agent writes but never updates STATUS.
            if elapsed_wake > HEARTBEAT_SECONDS:
                status_label = status if status else "MISSING"
                ava.agents.send_message(
                    ava.self.AGENT_ID,
                    f"coding agent heartbeat: STATUS is '{status_label}' in {path}, "
                    f"last file change was {elapsed_change:.0f}s ago -- "
                    "read that file and check progress",
                )
                return
        else:
            # Unknown STATUS token — wake so the agent can investigate.
            ava.agents.send_message(
                ava.self.AGENT_ID,
                f"coding agent reported unknown STATUS: '{status}' in {path} -- "
                "read that file and investigate",
            )
            return

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    watch(WORK_FILE)

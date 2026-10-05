"""Supervise a coding agent through its durable work file.

Generic use wakes the launching Ava agent on actionable status or a stall.
Canonical Codex use additionally owns terminal cleanup: DONE, HANDOFF, owner
termination, process death, expiry, or work-file deletion closes the recorded
PTY and reclaims its generation state directory before this process exits.

Delivery survives a restart window: each wake send retries with doubling gaps
(10s to a 160s cap, ~10.5 min in total) because a gateway / agent restart
window (an update wave, the fleet update) outlasts the SDK's own 3 quick
retries; a wake that never lands exits 2 at the one-shot call sites, while the
supervision loop keeps its schedule and retries at its next trigger.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from pathlib import Path

import ava
from base.agents import AgentNotFound, AgentStatus, GatewayUnavailable
from base.sessions import coding_session_owner
from base.sessions.coding_session_owner_record import CodingSessionStatus

WORK_FILE = "/path/to/work.md"
POLL_SECONDS = 60
STALL_SECONDS = 600
HEARTBEAT_SECONDS = 480
# 2h: a supervision run is bounded — after this the launching agent is woken
# with the limit notice and may relaunch; without it a forgotten runner polls
# forever (task #3696 exception inventory).
HARD_LIMIT_SECONDS = 7200
WAKE_ATTEMPTS = 8  # wake delivery tries (first + 7 retries); the gaps below
# sum to ~10.5 min — long enough to ride out a gateway / agent restart window
# (wake-delivery retry contract; task #3696 exception inventory)
WAKE_BACKOFF_S = 10.0  # first gap between wake tries; doubles per retry
WAKE_BACKOFF_MAX_S = 160.0  # cap for one gap

ACTIONABLE = ("DONE", "NEED_INPUT", "HANDOFF")
_STATUS = re.compile(r"^STATUS:\s*(\w+)", re.MULTILINE)


def read_status(path: str) -> str | None:
    """Return the first STATUS token, or None when absent/unreadable."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    matches = _STATUS.findall(text)
    return matches[0] if matches else None


def terminal_reason(
    status: str | None,
    *,
    status_is_current: bool,
    owner_terminated: bool,
    session_crashed: bool,
    expired: bool,
    work_file_deleted: bool,
    hard_limit_reached: bool,
) -> str | None:
    """Pure terminal-decision contract, ordered by semantic owner intent."""
    if status_is_current and status == "DONE":
        return "collaboration-done"
    if status_is_current and status == "HANDOFF":
        return "collaboration-handoff"
    if owner_terminated:
        return "owner-terminated"
    if session_crashed:
        return "session-crashed"
    if expired:
        return "expired"
    if work_file_deleted:
        return "work-file-deleted"
    if hard_limit_reached:
        return "supervisor-hard-limit"
    return None


# True while the gateway is unreachable, so an outage that spans many polls is reported once and
# its end once.
_gateway_down = False


def _owner_terminated(agent_id: int) -> bool:
    global _gateway_down  # noqa: PLW0603 — one-shot latch
    try:
        terminated = ava.agents.get_status(agent_id) is AgentStatus.TERMINATED
    except AgentNotFound:
        terminated = True
    except GatewayUnavailable as exc:
        # Gateway unavailability is not proof of termination. The PTY liveness
        # and absolute expiry checks remain local and continue to protect it.
        if not _gateway_down:
            _gateway_down = True
            print(
                f"gateway unavailable checking owner agent {agent_id}; treating it as alive: {exc!r}",
                flush=True,
            )
        return False
    if _gateway_down:
        _gateway_down = False
        print("gateway reachable again", flush=True)
    return terminated


def _session_crashed(owner: coding_session_owner.CodingSessionOwner) -> bool:
    if owner.status == CodingSessionStatus.LAUNCHING:
        return coding_session_owner.launch_is_stale(owner)
    if owner.status != CodingSessionStatus.ACTIVE or owner.session_name is None:
        return False
    from base.sessions.backend import get_shell_backend

    return not get_shell_backend().has_session(owner.session_name)


def _notify(agent_id: int, content: str, *, canonical: bool) -> bool:
    """Deliver one supervision message, retrying across a restart window.

    Delivery retries with growing gaps; returns False when every attempt
    failed — the one-shot call sites exit 2 on that, while the in-loop call
    sites keep supervising and retry at their next trigger.
    """
    delay = WAKE_BACKOFF_S
    for attempt in range(1, WAKE_ATTEMPTS + 1):
        try:
            if canonical:
                ava.agents.send_system_note(agent_id, content, tag="task", resurrect=False)
            else:
                ava.agents.send_message(agent_id, content)
            return True
        except Exception as exc:  # any transport failure retries
            print(f"wake attempt {attempt}/{WAKE_ATTEMPTS} failed: {exc!r}", flush=True)
            if attempt < WAKE_ATTEMPTS:
                time.sleep(delay)
                delay = min(delay * 2, WAKE_BACKOFF_MAX_S)
    print(f"wake delivery failed after {WAKE_ATTEMPTS} attempts", flush=True)
    return False


def _canonical_context(
    *,
    cluster: str | None,
    workspace: str | None,
    generation: str | None,
    owner_agent_id: int | None,
) -> tuple[coding_session_owner.CodingSessionKey, str, int] | None:
    values = (cluster, workspace, generation, owner_agent_id)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError("canonical supervision requires cluster, workspace, generation, and owner")
    assert cluster is not None and workspace is not None  # noqa: S101
    assert generation is not None and owner_agent_id is not None  # noqa: S101
    key = coding_session_owner.canonical_key(workspace, tool="codex", cluster=cluster)
    return key, generation, owner_agent_id


def _terminalize(
    key: coding_session_owner.CodingSessionKey,
    generation: str,
    owner_agent_id: int,
    reason: str,
) -> bool:
    try:
        stopped = coding_session_owner.terminate_generation(key, generation, reason=reason)
    except Exception as exc:
        print(f"terminalizing generation {generation} ({reason}) failed: {exc!r}", flush=True)
        if reason != "owner-terminated":
            _notify(
                owner_agent_id,
                f"Codex cleanup failed for generation {generation}: {exc}",
                canonical=True,
            )
        return False
    if stopped and reason != "owner-terminated":
        _notify(
            owner_agent_id,
            f"Codex generation {generation} terminalized ({reason}).",
            canonical=True,
        )
    return stopped


def _baseline_first_actionable_poll(
    *,
    first_poll: bool,
    status: str | None,
    mtime: float | None,
    last_actionable_mtime: float | None,
) -> tuple[bool, float | None]:
    """Record a pre-existing actionable status as the arming baseline."""
    if not first_poll:
        return False, last_actionable_mtime
    first_poll = False
    # A pre-existing actionable status at arming time is the baseline, not a
    # new report: suppress the immediate wake and wait for a change.
    if status in ACTIONABLE:
        return first_poll, mtime
    return first_poll, last_actionable_mtime


def _generic_actionable_woke(
    target_agent: int,
    status: str,
    path: str,
    mtime: float | None,
    last_actionable_mtime: float | None,
    elapsed_wake: float,
) -> bool:
    """Notify for a new actionable state or its unchanged-status heartbeat."""
    if mtime != last_actionable_mtime:
        if not _notify(
            target_agent,
            f"coding agent reported STATUS: {status} in {path} -- read that file",
            canonical=False,
        ):
            raise SystemExit(2)
        return True
    if elapsed_wake > HEARTBEAT_SECONDS:
        if not _notify(
            target_agent,
            f"coding agent heartbeat: STATUS is {status!r} in {path} (unchanged since arming)",
            canonical=False,
        ):
            raise SystemExit(2)
        return True
    return False


class _Watch:
    """One polling session: what the watcher has seen so far and the wake decisions that follow."""

    def __init__(
        self,
        path: str,
        target_agent: int,
        canonical_context: tuple[coding_session_owner.CodingSessionKey, str, int] | None,
    ) -> None:
        self.path = path
        self.target_agent = target_agent
        self.canonical_context = canonical_context
        self.start_time = time.monotonic()
        self.last_change = time.monotonic()
        self.last_wake = time.monotonic()
        self.last_mtime: float | None = None
        self.last_actionable_mtime: float | None = None
        self.first_poll = True
        self.saw_work_file = Path(path).exists()

    def poll(self) -> bool:
        """One polling beat; True when the watch is over (the caller sleeps otherwise)."""
        path = self.path
        try:
            mtime: float | None = Path(path).stat().st_mtime
            self.saw_work_file = True
        except FileNotFoundError:
            mtime = None
        if mtime != self.last_mtime:
            self.last_mtime = mtime
            self.last_change = time.monotonic()

        status = read_status(path)
        elapsed_total = time.monotonic() - self.start_time
        elapsed_change = time.monotonic() - self.last_change
        elapsed_wake = time.monotonic() - self.last_wake
        if self.canonical_context is not None:
            return self._canonical_poll(status, mtime, elapsed_change, elapsed_wake)
        return self._generic_poll(status, mtime, elapsed_total, elapsed_change, elapsed_wake)

    def _canonical_poll(
        self, status: str | None, mtime: float | None, elapsed_change: float, elapsed_wake: float
    ) -> bool:
        assert self.canonical_context is not None  # noqa: S101
        key, expected_generation, canonical_owner = self.canonical_context
        path = self.path
        owner = coding_session_owner.read(key, expected_generation)
        if owner.generation != expected_generation or owner.status in (
            CodingSessionStatus.INACTIVE,
            CodingSessionStatus.TERMINAL,
            CodingSessionStatus.INVALID,
        ):
            return True
        status_is_current = bool(
            mtime is not None
            and owner.created_at is not None
            and mtime >= owner.created_at.timestamp()
        )
        reason = terminal_reason(
            status,
            status_is_current=status_is_current,
            owner_terminated=_owner_terminated(canonical_owner),
            session_crashed=_session_crashed(owner),
            expired=bool(
                owner.expires_at is not None and dt.datetime.now(dt.UTC) >= owner.expires_at
            ),
            work_file_deleted=self.saw_work_file and mtime is None,
            # Canonical supervision uses the generation's persisted expiry;
            # the generic watcher's shorter wake-only hard limit must not
            # silently shorten a task-adapted Codex lease.
            hard_limit_reached=False,
        )
        if reason is not None:
            return _terminalize(key, expected_generation, canonical_owner, reason)

        if status == "NEED_INPUT" and status_is_current and mtime != self.last_actionable_mtime:
            _notify(
                self.target_agent,
                f"coding agent reported STATUS: NEED_INPUT in {path} -- read the file and reply",
                canonical=True,
            )
            self.last_actionable_mtime = mtime
            self.last_wake = time.monotonic()
        elif elapsed_change > STALL_SECONDS and elapsed_wake > HEARTBEAT_SECONDS:
            _notify(
                self.target_agent,
                f"coding agent has made no work-file change for {elapsed_change:.0f}s: {path}",
                canonical=True,
            )
            self.last_wake = time.monotonic()
        return False

    def _wake_or_exit(self, message: str) -> bool:
        """A generic wake notification; a failed delivery exits the watcher with status 2."""
        if not _notify(self.target_agent, message, canonical=False):
            raise SystemExit(2)
        return True

    def _generic_poll(
        self,
        status: str | None,
        mtime: float | None,
        elapsed_total: float,
        elapsed_change: float,
        elapsed_wake: float,
    ) -> bool:
        path = self.path
        self.first_poll, self.last_actionable_mtime = _baseline_first_actionable_poll(
            first_poll=self.first_poll,
            status=status,
            mtime=mtime,
            last_actionable_mtime=self.last_actionable_mtime,
        )
        if status is None and self.saw_work_file and mtime is None:
            return self._wake_or_exit(
                f"work file deleted: {path} -- the coding agent may have removed its workspace"
            )
        if elapsed_total > HARD_LIMIT_SECONDS:
            return self._wake_or_exit(
                f"hard limit reached while polling {path} for over {HARD_LIMIT_SECONDS}s"
            )
        if status in ACTIONABLE:
            return _generic_actionable_woke(
                self.target_agent,
                status,
                path,
                mtime,
                self.last_actionable_mtime,
                elapsed_wake,
            )
        if status in (None, "WORKING"):
            if elapsed_change > STALL_SECONDS:
                return self._wake_or_exit(
                    f"coding agent has been WORKING with no change to {path} for over "
                    f"{STALL_SECONDS}s -- capture its screen"
                )
            if elapsed_wake > HEARTBEAT_SECONDS:
                label = status if status else "MISSING"
                return self._wake_or_exit(f"coding agent heartbeat: STATUS is {label!r} in {path}")
            return False
        return self._wake_or_exit(f"coding agent reported unknown STATUS: {status!r} in {path}")


def watch(
    path: str,
    *,
    cluster: str | None = None,
    workspace: str | None = None,
    generation: str | None = None,
    owner_agent_id: int | None = None,
) -> None:
    """Poll until generic wake or canonical generation terminalization."""
    canonical_context = _canonical_context(
        cluster=cluster,
        workspace=workspace,
        generation=generation,
        owner_agent_id=owner_agent_id,
    )
    target_agent = owner_agent_id if owner_agent_id is not None else ava.self.AGENT_ID
    session = _Watch(path, target_agent, canonical_context)
    while not session.poll():
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    watch(WORK_FILE)

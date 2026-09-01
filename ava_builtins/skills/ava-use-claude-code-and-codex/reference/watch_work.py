"""Supervise a coding agent through its durable work file.

Generic use wakes the launching Ava agent on actionable status or a stall.
Canonical Codex use additionally owns terminal cleanup: DONE, HANDOFF, owner
termination, process death, expiry, or work-file deletion closes the recorded
PTY and reclaims its generation-private ``CODEX_HOME`` before this process exits.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import re
import time
from pathlib import Path

import ava
from shared import coding_session_owner
from shared.agents import AgentNotFound, AgentStatus

WORK_FILE = "/path/to/work.md"
POLL_SECONDS = 60
STALL_SECONDS = 600
HEARTBEAT_SECONDS = 480
HARD_LIMIT_SECONDS = 7200

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


def _owner_terminated(agent_id: int) -> bool:
    try:
        return ava.agents.get_status(agent_id) is AgentStatus.TERMINATED
    except AgentNotFound:
        return True
    except Exception:
        # Gateway unavailability is not proof of termination. The PTY liveness
        # and absolute expiry checks remain local and continue to protect it.
        return False


def _session_crashed(owner: coding_session_owner.CodingSessionOwner) -> bool:
    if owner.status == "launching":
        return coding_session_owner.launch_is_stale(owner)
    if owner.status != "active" or owner.session_name is None:
        return False
    from shared.session_backend import get_shell_backend

    return not get_shell_backend().has_session(owner.session_name)


def _notify(agent_id: int, content: str, *, canonical: bool) -> None:
    if canonical:
        ava.agents.send_system_note(agent_id, content, tag="task", resurrect=False)
    else:
        ava.agents.send_message(agent_id, content)


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
        if reason != "owner-terminated":
            with contextlib.suppress(Exception):
                _notify(
                    owner_agent_id,
                    f"Codex cleanup failed for generation {generation}: {exc}",
                    canonical=True,
                )
        return False
    if stopped and reason != "owner-terminated":
        with contextlib.suppress(Exception):
            _notify(
                owner_agent_id,
                f"Codex generation {generation} terminalized ({reason}).",
                canonical=True,
            )
    return stopped


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
    start_time = time.monotonic()
    last_change = time.monotonic()
    last_wake = time.monotonic()
    last_mtime: float | None = None
    last_actionable_mtime: float | None = None
    saw_work_file = Path(path).exists()

    while True:
        try:
            mtime: float | None = Path(path).stat().st_mtime
            saw_work_file = True
        except FileNotFoundError:
            mtime = None
        if mtime != last_mtime:
            last_mtime = mtime
            last_change = time.monotonic()

        status = read_status(path)
        elapsed_total = time.monotonic() - start_time
        elapsed_change = time.monotonic() - last_change
        elapsed_wake = time.monotonic() - last_wake

        if canonical_context is not None:
            key, expected_generation, canonical_owner = canonical_context
            owner = coding_session_owner.read(key)
            if owner.generation != expected_generation or owner.status in (
                "inactive",
                "terminal",
                "invalid",
            ):
                return
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
                work_file_deleted=saw_work_file and mtime is None,
                # Canonical supervision uses the generation's persisted expiry;
                # the generic watcher's shorter wake-only hard limit must not
                # silently shorten a task-adapted Codex lease.
                hard_limit_reached=False,
            )
            if reason is not None:
                if _terminalize(key, expected_generation, canonical_owner, reason):
                    return
                time.sleep(POLL_SECONDS)
                continue

            if status == "NEED_INPUT" and status_is_current and mtime != last_actionable_mtime:
                _notify(
                    target_agent,
                    f"coding agent reported STATUS: NEED_INPUT in {path} -- read the file and reply",
                    canonical=True,
                )
                last_actionable_mtime = mtime
                last_wake = time.monotonic()
            elif elapsed_change > STALL_SECONDS and elapsed_wake > HEARTBEAT_SECONDS:
                _notify(
                    target_agent,
                    f"coding agent has made no work-file change for {elapsed_change:.0f}s: {path}",
                    canonical=True,
                )
                last_wake = time.monotonic()
            time.sleep(POLL_SECONDS)
            continue

        if status is None and saw_work_file and mtime is None:
            _notify(
                target_agent,
                f"work file deleted: {path} -- the coding agent may have removed its workspace",
                canonical=False,
            )
            return
        if elapsed_total > HARD_LIMIT_SECONDS:
            _notify(
                target_agent,
                f"hard limit reached while polling {path} for over {HARD_LIMIT_SECONDS}s",
                canonical=False,
            )
            return
        if status in ACTIONABLE:
            _notify(
                target_agent,
                f"coding agent reported STATUS: {status} in {path} -- read that file",
                canonical=False,
            )
            return
        if status in (None, "WORKING"):
            if elapsed_change > STALL_SECONDS:
                _notify(
                    target_agent,
                    f"coding agent has been WORKING with no change to {path} for over "
                    f"{STALL_SECONDS}s -- capture its screen",
                    canonical=False,
                )
                return
            if elapsed_wake > HEARTBEAT_SECONDS:
                label = status if status else "MISSING"
                _notify(
                    target_agent,
                    f"coding agent heartbeat: STATUS is {label!r} in {path}",
                    canonical=False,
                )
                return
        else:
            _notify(
                target_agent,
                f"coding agent reported unknown STATUS: {status!r} in {path}",
                canonical=False,
            )
            return
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    watch(WORK_FILE)

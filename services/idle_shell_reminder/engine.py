"""Pure state transitions for persistent-shell idle reminders."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

THRESHOLDS_S: tuple[float, ...] = (
    5 * 60.0,
    30 * 60.0,
    60 * 60.0,
    2 * 60 * 60.0,
    4 * 60 * 60.0,
    8 * 60 * 60.0,
    16 * 60 * 60.0,
    24 * 60 * 60.0,
)

# Converting one monotonic timestamp to epoch uses the wall/monotonic offset.
# That offset can move by tiny fractions between observations (and daemon
# restarts); values closer than this still describe the same output byte.
_IDLE_START_TOLERANCE_S = 0.001


@dataclass(frozen=True, slots=True)
class IdleObservation:
    """One live agent-owned shell's current idle fact."""

    name: str
    owner: int
    sdk_id: int
    idle_start: float | None


@dataclass(frozen=True, slots=True)
class SessionState:
    """Persistent reminder state for one full PTY session name."""

    owner: int
    idle_start: float | None
    level: int
    exempt: bool
    last_reminded_at: float | None
    last_reminder_inbound_id: int | None


@dataclass(frozen=True, slots=True)
class ReminderSession:
    """One due shell included in an owner's merged reminder."""

    name: str
    sdk_id: int
    idle_seconds: float


@dataclass(frozen=True, slots=True)
class OwnerReminder:
    """One inbound to an owner, merged across all shells due this tick."""

    owner: int
    sessions: tuple[ReminderSession, ...]
    content: str


def _normalized_idle_start(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return value


def _same_idle_period(previous: float | None, observed: float | None) -> bool:
    if previous is None or observed is None:
        return previous is observed
    return abs(previous - observed) <= _IDLE_START_TOLERANCE_S


def _updated_session(previous: SessionState | None, observation: IdleObservation) -> SessionState:
    idle_start = _normalized_idle_start(observation.idle_start)
    if previous is None or previous.owner != observation.owner:
        return SessionState(
            owner=observation.owner,
            idle_start=idle_start,
            level=0,
            exempt=False,
            last_reminded_at=None,
            last_reminder_inbound_id=None,
        )
    if _same_idle_period(previous.idle_start, idle_start):
        return previous
    # Activity creates a new idle period but does not revoke a standing
    # exemption: a kept shell remains exempt until the session itself ends.
    return replace(previous, idle_start=idle_start, level=0, last_reminded_at=None)


def _duration_text(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    if minutes < 1:
        return "不足 1 分钟"
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, remaining_minutes = divmod(minutes, 60)
    if hours < 24:
        suffix = f" {remaining_minutes} 分钟" if remaining_minutes else ""
        return f"{hours} 小时{suffix}"
    days, remaining_hours = divmod(hours, 24)
    suffix = f" {remaining_hours} 小时" if remaining_hours else ""
    return f"{days} 天{suffix}"


def _reminder_content(sessions: tuple[ReminderSession, ...]) -> str:
    lines = [
        f"shell {session.name}（id {session.sdk_id}）已闲置 "  # noqa: RUF001
        f"{_duration_text(session.idle_seconds)}；不需要请 "  # noqa: RUF001
        f"ava.shell.sessions.kill({session.sdk_id}) 关闭。"
        for session in sessions
    ]
    lines.append("仍需要吗？回复『保留』将把以上 shell 标记为常驻，不再提醒。")  # noqa: RUF001
    return "\n".join(lines)


def advance(
    state: dict[str, SessionState],
    *,
    now: float,
    observations: Iterable[IdleObservation],
    live_session_names: set[str],
    owner_alive: Callable[[int], bool],
    retained_reply_ids: Callable[[int, frozenset[int]], set[int]],
) -> tuple[dict[str, SessionState], tuple[OwnerReminder, ...]]:
    """Apply one tick and return the next state plus merged reminders due.

    A live session whose host could not answer can be absent from
    ``observations`` while remaining in ``live_session_names``; its state is
    preserved but never advanced. Names absent from the live set are dropped.
    """
    observed = tuple(observations)
    next_state = {
        name: replace(session) for name, session in state.items() if name in live_session_names
    }
    for observation in observed:
        next_state[observation.name] = _updated_session(
            next_state.get(observation.name), observation
        )

    candidate_ids_by_owner: dict[int, set[int]] = {}
    for session in next_state.values():
        if session.exempt or session.last_reminder_inbound_id is None:
            continue
        candidate_ids_by_owner.setdefault(session.owner, set()).add(
            session.last_reminder_inbound_id
        )
    for owner, inbound_ids in candidate_ids_by_owner.items():
        retained_ids = retained_reply_ids(owner, frozenset(inbound_ids))
        if not retained_ids:
            continue
        for name, session in tuple(next_state.items()):
            if session.owner == owner and session.last_reminder_inbound_id in retained_ids:
                next_state[name] = replace(session, exempt=True)

    alive_by_owner: dict[int, bool] = {}
    due_by_owner: dict[int, list[ReminderSession]] = {}
    for observation in observed:
        session = next_state[observation.name]
        if session.idle_start is None or session.exempt:
            continue
        if session.owner not in alive_by_owner:
            alive_by_owner[session.owner] = owner_alive(session.owner)
        if not alive_by_owner[session.owner]:
            continue
        level = min(max(session.level, 0), len(THRESHOLDS_S) - 1)
        baseline = (
            session.idle_start if session.last_reminded_at is None else session.last_reminded_at
        )
        if now < baseline + THRESHOLDS_S[level]:
            continue
        due_by_owner.setdefault(session.owner, []).append(
            ReminderSession(
                name=observation.name,
                sdk_id=observation.sdk_id,
                idle_seconds=max(0.0, now - session.idle_start),
            )
        )

    reminders: list[OwnerReminder] = []
    for owner in sorted(due_by_owner):
        sessions = tuple(sorted(due_by_owner[owner], key=lambda session: session.name))
        reminders.append(
            OwnerReminder(owner=owner, sessions=sessions, content=_reminder_content(sessions))
        )
    return next_state, tuple(reminders)


def record_reminder(
    state: dict[str, SessionState],
    reminder: OwnerReminder,
    *,
    inbound_id: int,
    reminded_at: float,
) -> None:
    """Advance only sessions whose merged reminder was delivered successfully."""
    for due in reminder.sessions:
        session = state[due.name]
        state[due.name] = replace(
            session,
            level=min(session.level + 1, len(THRESHOLDS_S) - 1),
            last_reminded_at=reminded_at,
            last_reminder_inbound_id=inbound_id,
        )

"""Agent-runtime face of the ava_code plugin — hooks and contributions.

Loaded only in the agent process: `agent.extensions` imports this module after
`plugin.py` on the full path (host boot / graph build). The plugin's SDK
**surface** — the `cwd` namespace, the file/shell/understand/ui wraps, and the
context-file injection — lives in `plugin.py`, which agent-launched children
load on their own (surface-only) boot (task #3633). The state class is in `_state.py`
and the prompt sections in `_prompt_sections.py`; this module imports no `ava`, because
its hooks run in the agent host and operate on the graph state they are handed.

The surface reaches this plugin's state through `plugin.read_state` /
`plugin.update_state`, which build the handle here at each call (a child upgrades
to this face before any state is injected — `agent/exec_child.py`; a stateful
request never takes the surface-only path).
"""

from __future__ import annotations

import stat
from datetime import UTC, datetime
from pathlib import Path

from agent.hooks import Hook
from agent.messages import NoteTag, system_note_message
from agent.state import AgentState, PluginStateHandle
from base.log import logger
from base.packages.plugins.extensions import PluginContributions

from ._prompt_sections import _coding_tools_section, _engineering_workflow_section
from ._state import AvaCodeState, default_cwd


def state_handle() -> PluginStateHandle[AvaCodeState]:
    """The typed handle over this plugin's state — a view over the turn's state slot, built per use."""
    return PluginStateHandle(AvaCodeState, "ava_code")


# ── after_exec hook: cwd + project-skills notes ──────────────────────────────
# AGENTS.md / CLAUDE.md context injection and security-finding delivery moved
# INTO the exec node's messages delta (in-memory, user ruling 2026-08-11) —
# this hook no longer touches the messages channel. It keeps the two note
# types that are summaries of plugin state rather than content discovered
# during exec: the cwd-change note and the project-skills listing.


class _InjectCwdNotesAfterExecHook(Hook):
    """Inject the cwd-change note and the project-skills note when
    `ava.cwd.set` left them pending; re-inject the skills note after compact.
    No-op when there is nothing to inject."""

    async def __call__(
        self,
        state: AgentState,
        _runtime: object,
        _config: object,
        /,
    ) -> dict | None:
        # The hook runs in the agent host: it reads the graph `state` it was handed through the
        # handle's pure `view`, and returns its writes as an update dict (`delta`) for the
        # LangGraph reducer — never the SDK's exec slot.
        current = state_handle().view(state)
        notes: list = []
        update: dict = {}

        # ── cwd-note injection ───────────────────────────────────────────────
        # cwd.set() writes a summary into cwd_note (path + optional project
        # skills listing). Inject it here as a system note on the first turn
        # after the cwd change. Same-turn dedup: cwd_note carries the *last*
        # set() value; the hook reads and clears it.
        if current.cwd_note is not None:
            notes.append(
                system_note_message(
                    content=current.cwd_note,
                    tag=NoteTag.CONTEXT,
                    created_at=datetime.now(UTC),
                )
            )
            update["cwd_note"] = None

        # ── project-skills note injection ───────────────────────────────────
        # When cwd.set() discovers project-local skills it writes the summary
        # into project_skills_note. Inject it here as a system note on the
        # first turn after the cwd change and re-inject after compact (same
        # lazy-reset pattern as injected_paths: compact.version advancing past
        # the bookmark clears the "already injected" guard).
        # project_skills_seen_compact defaults to -1 so the first injection
        # always fires (compact.version starts at 0).
        if current.project_skills_note is not None:
            compact_v = state.compact.version
            if compact_v > current.project_skills_seen_compact:
                notes.append(
                    system_note_message(
                        content=current.project_skills_note,
                        tag=NoteTag.PROJECT_SKILLS,
                        created_at=datetime.now(UTC),
                    )
                )
                update["project_skills_seen_compact"] = compact_v

        if notes:
            logger.info("[ava_code] injecting {} finding(s) as system notes", len(notes))
            update["messages"] = notes
            return state_handle().delta(update)
        return None


# ── after_init hook: validate persisted logical cwd ─────────────────────
def _logical_cwd_error(cwd: Path) -> OSError | None:
    """Return why ``cwd`` cannot serve as a logical directory, or None."""
    try:
        mode = cwd.stat().st_mode
    except OSError as exc:
        return exc
    if not stat.S_ISDIR(mode):
        return NotADirectoryError(f"persisted ava.cwd is not a directory: {cwd}")
    return None


class _ValidateCwdAfterInitHook(Hook):
    """Repair a persisted logical cwd that cannot be statted or is not a directory.

    The Python process cwd is deliberately outside plugin state: SDK wrappers
    resolve against ``ava.cwd`` explicitly, while bare Python filesystem and
    subprocess calls retain the process's stable startup cwd.
    """

    async def __call__(
        self,
        state: AgentState,
        _runtime: object,
        _config: object,
        /,
    ) -> dict | None:
        cwd = Path(state.ava_code__cwd)  # pyright: ignore[reportAttributeAccessIssue]
        exc = _logical_cwd_error(cwd)
        if exc is not None:
            # The persisted cwd is no longer usable (worktree deleted after
            # PR merge / task cleanup, drive unmounted, replaced by a file,
            # etc.). Fall back
            # to the agent's workspace and persist the new cwd so future
            # turns and restarts don't crash on the same stale path.
            fallback = default_cwd()
            logger.warning(
                "[ava_code] after_init: persisted cwd {cwd!r} failed validation "
                "({exc!r}), falling back to {fallback!r} — state updated so "
                "future restarts use the new cwd",
                cwd=str(cwd),
                exc=exc,
                fallback=str(fallback),
            )
            return {"ava_code__cwd": str(fallback)}
        return None


def contribute() -> PluginContributions:
    """What this plugin declares for the agent runtime."""
    return PluginContributions(
        system_prompt_sections=(_coding_tools_section, _engineering_workflow_section),
        after_init=(_ValidateCwdAfterInitHook(),),
        after_exec=(_InjectCwdNotesAfterExecHook(),),
        state=(AvaCodeState,),
    )

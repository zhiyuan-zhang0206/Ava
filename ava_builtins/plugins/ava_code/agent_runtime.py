"""Agent-runtime face of the ava_code plugin — state field, prompt sections, hooks.

Loaded only in the agent process: `agent._extensions` imports this module after
`plugin.py` on the full path (host boot / graph build). The plugin's SDK
**surface** — the `cwd` namespace, the file/shell/understand/ui wraps, and the
context-file injection — lives in `plugin.py`, which agent-launched children
load on their own (surface-only) boot (task #3633).

The real `PluginStateHandle` is created here and rebound onto the surface
module (`plugin.state_handle`). Until then the surface holds a stand-in that
raises the same `PluginStateOutsideTurnError` the real handle raises outside
an exec turn; a child upgrades to this face before any state is injected
(`agent/exec_child.py` — a stateful request never takes the surface-only
path).
"""

from __future__ import annotations

import contextlib
import io
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

import ava
import ava.agent_identity as _ava_identity
from agent.graph._system_prompt import register_system_prompt_section
from agent.hooks import Hook, register_after_exec, register_after_init
from agent.messages import NoteTag, system_note_message
from agent.state import AgentState, register_plugin_state
from shared.config.turn_view import turn_settings
from shared.log import logger
from shared.paths import workspace_dir

from . import plugin as _surface


# ── state field declaration ──────────────────────────────────────────────
def _default_cwd() -> str:
    """Initial cwd for a fresh agent state: the agent's own workspace.

    State is first created inside a bootstrapped agent process, after
    `ava.agent_identity.establish` has bound the identity — so a real run starts in
    `$AVA_HOME/workspaces/<agent_id>/` (created here on first touch). Direct
    state construction without a bootstrap (tests, dev REPL) has no agent and
    therefore no workspace; $HOME is the documented pre-bootstrap placeholder
    (see `ava.agent_identity.agent_id`)."""
    aid = _ava_identity.agent_id()
    if aid is None:  # pyright: ignore[reportUnnecessaryComparison] — agent_id() returns None pre-bootstrap
        return str(Path.home())
    return str(workspace_dir(aid))


class AvaCodeState(BaseModel):
    """plugins.ava_code persistent state — survives across turns/restarts via LangGraph checkpoint.

    - cwd: agent-maintained working directory, default `_default_cwd()` (the
      agent's workspace); the agent switches it via `ava.cwd.set`; the
      `ava.files.read` wrap uses it to resolve relative paths.
    - messages: declared base channel (exact BaseAgentState annotation) —
      the context-file notes this plugin appends during the exec turn; the
      exec node merges them into its own messages delta (after the exec
      result). See the field comment below.
    - injected_paths: set of context-file (AGENTS.md / CLAUDE.md) paths already
      surfaced to the agent, deduped to prevent the wrap from re-injecting
      (both auto-inject and direct agent reads mark into here; see module
      docstring + `_wrapped_read` comment).
    - last_seen_compact: bookmark compared against the built-in
      `compact.version`. After compact strips messages, injected_paths is
      lazily cleared (detected at wrap entry, not actively reset) — the same
      monotonic version-counter reset `ava_sdk_reminder` uses.
    - project_skills_note: the project-local skills summary string to inject
      as a system note when cwd changes. None when cwd has not been set or
      the cwd is not under a git repo with project skills. Set by
      `ava.cwd.set()` and injected by the after-exec hook.
    - project_skills_seen_compact: compact version bookmark for re-injection
      after compaction. When compact.version advances past this bookmark the
      note is re-injected (same lazy-reset pattern as injected_paths).
      Defaults to -1 so the first injection always fires (compact.version
      starts at 0).
    - cwd_note: set by ava.cwd.set() to trigger a system note injection in
      the after-exec hook. The hook reads and clears it, injecting
      "[system] Working directory set to ..." with optional project-skills
      listing. Per-turn dedup: subsequent cwd.set() calls overwrite the
      field; only the final value is injected.
    """

    cwd: str = Field(default_factory=_default_cwd)
    # Base-channel declaration: this plugin legitimately appends system notes
    # (AGENTS.md / CLAUDE.md context injection) to the framework's `messages`
    # channel during the exec turn. The annotation must match BaseAgentState
    # exactly (incl. the add_messages reducer) — register_plugin_state
    # enforces it — and the exec node merges the plugin's messages delta with
    # its own ToolMessage delta (agent/graph/_exec.py), so the notes ride in
    # the same in-memory state update instead of a side-channel file.
    messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)
    injected_paths: set[str] = Field(default_factory=set)
    injected_hashes: set[str] = Field(default_factory=set)
    last_seen_compact: int = 0
    project_skills_note: str | None = None
    project_skills_seen_compact: int = -1
    cwd_note: str | None = None


state_handle = register_plugin_state(AvaCodeState)

# The surface module holds a stand-in handle until this face loads — on the
# agent side the face loads at boot; in a stateful child the lazy state slot
# materializes on first handle use (task #3633 leg-2). Either way the rebind
# lets the surface's call sites (`ava.cwd.get`/`set`, the read wrap's injection
# path, the project-skill source) share the real handle.
_surface.state_handle = state_handle


# ── system prompt section: coding tool advertising ──────────────────────────
# From the plugin's perspective, what "hands" should the agent use for
# coding? Feed cwd / files / shell as three modules into help() — the
# renderer is Python-stub-format, each module renders the full docstring +
# each child function as `def name(sig): """doc"""`; the agent gets the
# full contract with zero drill-down.

_PROMOTED_MODULES = ("cwd", "files", "shell")


@register_system_prompt_section
def _coding_tools_section() -> str:
    """Render the cwd / files / shell modules as Python stubs under `## ava.X`.

    A module already expanded by the framework's "Expanded SDK reference"
    section (the effective list: plugin registrations + AVA_SDK_EXPAND, exact
    path match) is skipped here — its full contract is in the prompt once
    already; the preamble conventions below still apply and are always
    rendered. With the default config every promoted module is expanded, so
    this section reduces to the preamble."""
    from agent.graph._system_prompt import effective_sdk_expand

    expanded = set(effective_sdk_expand())
    pieces: list[str] = []
    for name in _PROMOTED_MODULES:
        if name in expanded:
            continue
        mod = getattr(ava, name)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ava.help(mod)
        pieces.append(buf.getvalue().rstrip())
    body = "\n\n".join(pieces)
    preamble = (
        "**Prefer the tools below** for file and shell operations. "
        "When you start a coding task:\n\n"
        "- Point your working directory at the project root once when you start.\n"
        "- Read `AGENTS.md` first (via the file tools) before reading other "
        "code — it sets the project rules you must follow.\n"
        "- Search with `rg` (ripgrep), not `grep -r`/`find` — recursive grep scans "
        "every `.worktrees/` checkout and often hits the 30s shell timeout "
        "(benchmarks in `ava-code:conventions`).\n"
        "- For any change to a repo, work in an isolated `git worktree` — never edit, "
        "switch the branch of, or push the shared checkout directly; every change goes "
        "through a PR. Read the `worktree` skill for naming and how to create one.\n"
        "- Every commit appends a `Co-authored-by: Ava #<your agent id>` trailer; PR "
        "titles start with `[Ava-<your agent id>]`.\n"
        "- Before calling a change done, run the narrowest check that proves it (the "
        "failing test, the exact command), then widen to nearby cases — don't claim "
        "success from reading the diff alone.\n"
        "- `execute_code` has a hard timeout (default 300s); keep it to short, quick "
        "work. Run anything long-running (e.g. `time.sleep`, large downloads, long "
        "polling) in an `ava.shell.sessions` persistent shell instead."
    )
    if not body:
        return f"# Coding tools\n\n{preamble}"
    return f"# Coding tools\n\n{preamble}\n\n{body}"


# ── system prompt section: debugging workflow (opt-in) ──────────────────────
# Coding-specific advice, so it belongs to this plugin rather than the core
# prompt. Off by default; toggled by adding "ava_code_workflow" to
# settings.agent.system_prompt_extra (env AVA_SYSTEM_PROMPT_EXTRA).
# Empty return when disabled = no contribution.
@register_system_prompt_section
def _engineering_workflow_section() -> str:
    """Loose bug-fix-workflow advice, gated by system_prompt_extra=ava_code_workflow."""
    if "ava_code_workflow" not in turn_settings.agent.system_prompt_extra:
        return ""
    return (
        "## Resolving issues and debugging\n\n"
        "When you're trying to resolve an issue or chase down a bug, start by "
        "reproducing the failure. That's the foundation — without a reliable "
        "repro you're guessing at causes.\n\n"
        "Once you have it, push further. The failing case in front of you is "
        "one way the bug shows up; the same underlying defect usually has "
        "several other manifestations. As you reproduce, keep asking: what "
        "other inputs hit this code path? What adjacent edge cases would also "
        "fail? The point is to map the space of failures, not confirm a "
        "single instance.\n\n"
        "Reproduction and root-cause analysis feed each other. Each step "
        "deeper into the cause reveals new failing inputs; each new failing "
        "input sharpens the cause. Treat them as iteration, not two "
        "sequential phases.\n\n"
        "Before believing your fix is done, run it against the variations "
        "you've discovered, not just the original case. A fix that handles "
        "one instance but doesn't address the broader pattern is usually the "
        "wrong fix.\n"
    )


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
        notes: list = []

        # ── cwd-note injection ───────────────────────────────────────────────
        # cwd.set() writes a summary into cwd_note (path + optional project
        # skills listing). Inject it here as a system note on the first turn
        # after the cwd change. Same-turn dedup: cwd_note carries the *last*
        # set() value; the hook reads and clears it.
        #
        # ── project-skills note injection ───────────────────────────────────
        # When cwd.set() discovers project-local skills it writes the summary
        # into project_skills_note. Inject it here as a system note on the
        # first turn after the cwd change and re-inject after compact (same
        # lazy-reset pattern as injected_paths: compact.version advancing past
        # the bookmark clears the "already injected" guard).
        # project_skills_seen_compact defaults to -1 so the first injection
        # always fires (compact.version starts at 0).
        result: dict = {}
        with contextlib.suppress(ava.PluginStateOutsideTurnError):
            current = state_handle.read()
            cwd_note = current.cwd_note
            if cwd_note is not None:
                notes.append(
                    system_note_message(
                        content=cwd_note,
                        tag=NoteTag.CONTEXT,
                        created_at=datetime.now(UTC),
                    )
                )
                result["ava_code__cwd_note"] = None
            skills_text = current.project_skills_note
            if skills_text is not None:
                compact_v = state.compact.version
                if compact_v > current.project_skills_seen_compact:
                    notes.append(
                        system_note_message(
                            content=skills_text,
                            tag=NoteTag.PROJECT_SKILLS,
                            created_at=datetime.now(UTC),
                        )
                    )
                    result["ava_code__project_skills_seen_compact"] = compact_v

        if notes:
            logger.info("[ava_code] injecting %d finding(s) as system notes", len(notes))
            result["messages"] = notes
            return result
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
            fallback = _default_cwd()
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


validate_cwd_after_init = _ValidateCwdAfterInitHook()
register_after_init(validate_cwd_after_init)

inject_cwd_notes_after_exec = _InjectCwdNotesAfterExecHook()
register_after_exec(inject_cwd_notes_after_exec)

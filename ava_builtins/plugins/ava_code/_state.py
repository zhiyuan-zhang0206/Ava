"""The ava_code plugin's state class — the `ava_code__*` graph channels and their defaults.

The cwd factory serves direct child-side state construction. The host's after-init hook
initializes an absent graph channel from the invocation's explicit Runtime context.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

import ava.sdk_surface.agent_identity as _ava_identity
from base.paths import workspace_dir


# ── state field declaration ──────────────────────────────────────────────
def default_cwd() -> str:
    """Initial cwd for a fresh agent state: the agent's own workspace.

    Direct construction in an execution child uses its local SDK identity.
    Without that identity, the schema default is the $HOME placeholder.
    In the host, after_init initializes the absent cwd channel from Runtime
    and persists that value; this schema default is not the host's authority.
    """
    aid = _ava_identity.agent_id()
    if aid is None:
        return str(Path.home())
    return str(workspace_dir(aid))


class AvaCodeState(BaseModel):
    """plugins.ava_code persistent state — survives across turns/restarts via LangGraph checkpoint.

    - cwd: agent-maintained working directory, default `default_cwd()` (the
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

    cwd: str = Field(default_factory=default_cwd)
    # Base-channel declaration: this plugin legitimately appends system notes
    # (AGENTS.md / CLAUDE.md context injection) to the framework's `messages`
    # channel during the exec turn. The annotation must match BaseAgentState
    # exactly (incl. the add_messages reducer) — the registry's state
    # validation enforces it — and the exec node merges the plugin's messages delta with
    # its own ToolMessage delta (agent/graph/exec/node.py), so the notes ride in
    # the same in-memory state update instead of a side-channel file.
    messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)
    injected_paths: set[str] = Field(default_factory=set)
    injected_hashes: set[str] = Field(default_factory=set)
    last_seen_compact: int = 0
    project_skills_note: str | None = None
    project_skills_seen_compact: int = -1
    cwd_note: str | None = None

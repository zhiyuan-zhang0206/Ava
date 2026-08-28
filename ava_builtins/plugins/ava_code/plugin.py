"""Ava code plugin — coding-agent conventions + automatic context-file injection.

Two pieces, cwd and context-file injection:
- cwd: `ava_code__cwd` state field (default: the agent's workspace dir; $HOME
  when no process identity is bound); the agent reads/writes via
  `ava.cwd.get` / `set`; the wraps use it to resolve relative paths in
  `ava.files.*` / `ava.shell.run` / each `ava.understand` target's `paths` /
  `ava.ui.serve`'s `dir`.
- context-file auto-injection: wraps `ava.files.read`, walks up the resolved
  path to git root or `$HOME` (whichever is farther), and surfaces any AGENTS.md
  / CLAUDE.md files along the way as a system note (tag=CONTEXT) so the agent
  sees them next turn. Delivery is **in-memory, inside the exec turn** (user
  ruling 2026-08-11 — the old side-channel JSONL file is gone): the wrap
  appends each note to the base `messages` channel via `state_handle.update`,
  and the exec node (agent/graph/_exec.py) merges the plugin's messages delta
  with its own exec-result ToolMessage, after it (the Anthropic-compat wire
  contract forbids notes between the AIMessage and its ToolMessage). No file
  is written except the overflow archive for oversized context files
  (truncated head+tail inline, full text under the workspace `.exec_output/`
  ring — the same logic as exec output overflow). **The wrap is a fallback**:
  the system prompt tells the agent to first `ava.files.read("AGENTS.md")`;
  when the agent goes through that primary path, the wrap marks that path into
  `injected_paths` but does not inject it (the content is already returned to
  the agent). Dedup uses `injected_paths: set[str]` plugin state; after compact
  strips messages, it is lazily reset via `last_seen_compact` <
  `compact.version`.

When the wrap is called outside an exec turn (test / dev REPL), it fast-paths
straight through to the original read — no path rewriting, no injection.
Behavior is identical to the original SDK function, avoiding a silent fallback
to the system cwd that would diverge from plugin-enabled behavior.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated, Any

__description__ = "Ava Code conventions — maintains cwd and auto-injects project AGENTS.md / CLAUDE.md (walking up from ava.files.read paths)"

import contextlib
import io
import stat
from collections.abc import Callable
from pathlib import Path

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

import ava
import ava._boot as _ava_boot
import ava.files as _ava_files_mod
import ava.skills as _ava_skills
from agent.graph._exec_output import truncate_both_ends
from agent.graph._system_prompt import register_system_prompt_section
from agent.hooks import Hook, register_after_exec, register_after_init
from agent.messages import NoteTag, system_note_message
from agent.state import AgentState, register_plugin_state
from ava._sdk_validation import coerce_str
from ava.security import scan_content
from shared.config import settings
from shared.config.turn_view import turn_settings
from shared.log import logger
from shared.paths import workspace_dir

from . import _code_namespace
from ._walk import find_context_files_along_path, project_skill_roots

_files_resolve = _ava_files_mod._resolve

# ── ava.cwd SDK namespace registration — must run before register_plugin_state
# so a plugin double-load (test fixture / dev hot-reload) hits the namespace
# conflict first (`PluginNamespaceConflictError`, PR #192's first line of
# defense) rather than the state-field reducer-function-identity-mismatch
# annotation conflict below (function objects differ after reload).
# _code_namespace's top level does not depend on state_handle (function
# bodies lazy-import), so importing here is cycle-free.
ava.register_namespace("cwd", _code_namespace)
# Promote cwd into the system prompt's expanded SDK reference, ahead of the
# configured framework list — it is the top coding surface, and a framework
# default cannot name a plugin namespace (issue #1011).
ava.register_sdk_expand("cwd")


# ── state field declaration ──────────────────────────────────────────────
def _default_cwd() -> str:
    """Initial cwd for a fresh agent state: the agent's own workspace.

    State is first created inside a bootstrapped agent process, after
    `ava._boot.establish` has bound the identity — so a real run starts in
    `$AVA_HOME/workspaces/<agent_id>/` (created here on first touch). Direct
    state construction without a bootstrap (tests, dev REPL) has no agent and
    therefore no workspace; $HOME is the documented pre-bootstrap placeholder
    (see `ava._boot.agent_id`)."""
    aid = _ava_boot.agent_id()
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


# ── project-local skill source ───────────────────────────────────────────
# Contribute the working repo's project-skill folders (resolved from cwd at
# scan time) so they surface under `ava.skills.*` like any other skill. The
# scan runs on every lookup AND once at the system-prompt build, where state
# is not yet bound (cwd still default) — there we contribute nothing.
def _project_skill_source() -> list[Path]:
    try:
        cwd = Path(state_handle.read().cwd)
    except ava.PluginStateOutsideTurnError:
        return []
    return project_skill_roots(cwd)


# When ava.skills is disabled via AVA_SDK_DISABLE, project-local skill
# source registration is unavailable; the core plugin (cwd, file wraps)
# continues to function normally.
with contextlib.suppress(AttributeError):
    _ava_skills.register_skill_source(_project_skill_source)


# ── wrap ava.files.read ───────────────────────────────────────────────────
# Registered through `ava.extend.wrap` at the bottom of this section. The
# `inner` parameter is the current `ava.files.read` (the original, or another
# plugin's wrap when several stack); context-file dedup counts injections, not
# wrap layers, so calling `inner` exactly once per read keeps the count right no
# matter how many layers sit below.


def _process_context_file(
    ctx_file: Path,
    target: Path,
    *,
    injected: set[str],
    hashes: set[str],
) -> bool:
    """Process one candidate context file for auto-injection.

    Returns True when the caller should continue to the next candidate
    (file was handled: injected, skipped via dedup, or errored).
    Returns False when the file was not a context file (should not happen).
    """
    ctx_file_str = str(ctx_file)
    if ctx_file_str in injected:
        return True
    # Primary-path check: if the target read is this context file itself,
    # the content reaches the agent via the return value — do not auto-inject.
    # Still mark into injected to prevent a later sibling read from injecting,
    # and hash the content so a same-content copy from another path is also blocked.
    try:
        is_target = ctx_file.samefile(target)
    except OSError:
        is_target = False
    if is_target:
        injected.add(ctx_file_str)
        try:
            target_content = ctx_file.read_text(encoding="utf-8")
        except (PermissionError, FileNotFoundError, UnicodeDecodeError):
            return True
        hashes.add(hashlib.sha256(target_content.encode()).hexdigest())
        return True
    try:
        content = ctx_file.read_text(encoding="utf-8")
    except (PermissionError, FileNotFoundError) as e:
        logger.warning("[ava_code] skip context file {p}: {e}", p=ctx_file, e=e)
        return True
    except UnicodeDecodeError as e:
        logger.warning("[ava_code] context file {p} is not utf-8: {e}", p=ctx_file, e=e)
        return True
    # Skip empty files — no content to surface to the agent.
    if not content.strip():
        return True
    # Hash-based dedup: same content from a different path must not be
    # injected twice (the worktree vs main-repo AGENTS.md case).
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    if content_hash in hashes:
        injected.add(ctx_file_str)
        return True
    # Injection-pattern scan: records a SECURITY finding (delivered as a
    # system note by the exec node) when the content carries injection
    # patterns; the content itself is returned clean.
    scan_content(content, source=f"context-file:{ctx_file}")
    # Deliver the context file as a system note in THIS exec's messages
    # delta (in-memory — no side-channel file). Oversized content is
    # truncated head+tail with the full text archived to the workspace
    # .exec_output/ ring, same logic as the exec node's output overflow
    # (user ruling 2026-08-11: files only for archiving / oversized
    # content); the archive path rides in the injected note.
    note_body = content
    if len(note_body) > settings.sandbox.exec_output_max_chars:
        note_body = truncate_both_ends(note_body, settings.sandbox.exec_output_max_chars)
    state_handle.update(
        {
            "messages": [
                system_note_message(
                    content=f"Project {ctx_file.name} from {ctx_file_str}:\n\n{note_body}",
                    tag=NoteTag.CONTEXT,
                    created_at=datetime.now(UTC),
                )
            ]
        }
    )
    hashes.add(content_hash)
    injected.add(ctx_file_str)
    return True


def _wrapped_read(
    inner: Callable[..., str],
    path: str | Path,
    start: int | None = None,
    end: int | None = None,
    *,
    limit: int | None = None,
    with_line_numbers: bool = False,
) -> str:
    """Read a file, or a 1-indexed inclusive line range.

    `limit` (max lines from `start`) is mutually exclusive with `end`.
    Project `AGENTS.md` / `CLAUDE.md` files found along the resolved path
    are surfaced as a system note once each.
    """
    # Audience=agent. Dev-perspective implementation (cwd resolution /
    # context-file walk / dedup / fast-path) is documented in plugin.py's
    # module docstring. This wrapper takes over `ava.files.read`'s namespace
    # entry; the docstring above (kept by `ava.extend.wrap`) is the read
    # contract. `inner` is the wrapped `ava.files.read`.

    # Normalize before path rewriting: a trailing-comma tuple must become
    # the string it wraps, or the cwd resolution below would str() it into
    # a garbage filename.
    path = coerce_str(path, "path", allow_types=(Path,))

    # Fast-path: when called outside a turn (test / dev REPL), pass through
    # to the wrapped read — no path rewriting, no injection.
    if ava.state is None:
        return inner(path, start, end, limit=limit, with_line_numbers=with_line_numbers)

    # 1. Resolve path against plugin cwd
    p = Path(path).expanduser()
    cwd = _code_namespace.get()
    p = (cwd / p).resolve() if not p.is_absolute() else p.resolve()

    # 2. Walk up the path collecting context files (AGENTS.md / CLAUDE.md)
    candidates = find_context_files_along_path(p)

    # 3. Lazy compact reset: each successful compaction bumps compact.version.
    # This wrap entry compares against the bookmark; if the version advanced,
    # clear injected_paths — the corresponding context-file content in messages
    # has been replaced by a summary and needs to be re-surfaced to the agent.
    # compact is a built-in BaseAgentState sub-state (always present), so read
    # it directly — a missing value is a real bug, not a default-to-0 case.
    current = state_handle.read()
    compact_v: int = ava.state.compact.version
    if compact_v > current.last_seen_compact:
        injected: set[str] = set()
        hashes: set[str] = set()
        new_bookmark = compact_v
    else:
        injected = set(current.injected_paths)
        hashes = set(current.injected_hashes)
        new_bookmark = current.last_seen_compact

    # 4. Inject / mark walk results. For multiple reads in the same turn,
    # state_handle.read() reflects values updated earlier (handle.update
    # synchronously mutates the ava.state working copy); no extra cache needed.
    for ctx_file in candidates:
        _process_context_file(ctx_file, p, injected=injected, hashes=hashes)

    # 5. Commit state. injected_paths has no reducer, so it follows
    # last-value semantics — the wrap is the sole writer, so just pass the
    # full new set.
    update_dict: dict = {}
    if injected != current.injected_paths:
        update_dict["injected_paths"] = injected
    if hashes != current.injected_hashes:
        update_dict["injected_hashes"] = hashes
    if new_bookmark != current.last_seen_compact:
        update_dict["last_seen_compact"] = new_bookmark
    if update_dict:
        state_handle.update(update_dict)

    # 6. Actual read — pass the plugin-cwd-resolved absolute path; do not rely
    # on the system cwd. Line-range params thread straight through.
    return inner(str(p), start, end, limit=limit, with_line_numbers=with_line_numbers)


ava.extend.wrap("files.read", _wrapped_read)


# ── wrap ava.shell.run ────────────────────────────────────────────────────
# Inject the plugin-tracked cwd so the agent's one-off commands run in the
# working directory by default, instead of having to `cd <path> && ...` every
# time. `inner` is the wrapped `ava.shell.run`.
def _wrapped_shell_run(
    inner: Callable[..., str],
    cmd: str,
    *,
    cwd: str | None = None,
    timeout: float = 30.0,
) -> str:
    """Non-zero exit does not raise; the command is killed after `timeout`
    seconds. The returned string carries read-only `.returncode`
    (0 = success) and `.stderr`; string operations on it return a plain
    `str` without them.

    Runs in your tracked working directory (`ava.cwd`) unless `cwd` is passed.
    """
    # Same by-design fast-path as _wrapped_read: outside a turn (test / dev
    # REPL), pass through; do not depend on plugin cwd state.
    if ava.state is None or cwd is not None:
        return inner(cmd, cwd=cwd, timeout=timeout)
    return inner(cmd, cwd=str(_code_namespace.get()), timeout=timeout)


ava.extend.wrap("shell.run", _wrapped_shell_run)


# ── wrap ava.files.edit / write / append / delete / glob ──────────────────
# The SDK core (ava.files._resolve) resolves relative paths against the
# agent's workspace; these wraps layer cwd *tracking* on top — after
# `ava.cwd.set("<repo>")` every file op follows the tracked cwd instead of
# staying pinned to the workspace.
# Uniform wrap: when a turn is active, fast-path cwd resolution matches
# _wrapped_read; outside a turn (ava.state is None) pass through to the
# original function.
def _resolve_for_cwd(path: str | Path) -> Path:
    """Resolve path against plugin cwd; outside a turn defer to the SDK core
    resolution (workspace, or HOME before an identity is bound)."""
    if ava.state is None:
        return _files_resolve(str(path))
    p = Path(path).expanduser()
    cwd = _code_namespace.get()
    return (cwd / p).resolve() if not p.is_absolute() else p.resolve()


# These wraps only change path resolution, so none carries its own docstring —
# `ava.extend.wrap` keeps the wrapped function's contract when the wrapper has
# none, so the rendered SDK stub still shows the original `files.*` docstrings.
# `inner` is the wrapped op.


# ── edit ──
def _wrapped_edit(
    inner: Callable[..., None], path: str | Path, old: str, new: str, *, replace_all: bool = False
) -> None:
    path = coerce_str(path, "path", allow_types=(Path,))
    p = _resolve_for_cwd(path)
    return inner(str(p), old, new, replace_all=replace_all)


ava.extend.wrap("files.edit", _wrapped_edit)


# ── write ──
def _wrapped_write(inner: Callable[..., None], path: str | Path, content: str) -> None:
    path = coerce_str(path, "path", allow_types=(Path,))
    p = _resolve_for_cwd(path)
    return inner(str(p), content)


ava.extend.wrap("files.write", _wrapped_write)


# ── append ──
def _wrapped_append(inner: Callable[..., None], path: str | Path, content: str) -> None:
    path = coerce_str(path, "path", allow_types=(Path,))
    p = _resolve_for_cwd(path)
    return inner(str(p), content)


ava.extend.wrap("files.append", _wrapped_append)


# ── delete ──
def _wrapped_delete(inner: Callable[..., None], path: str | Path) -> None:
    path = coerce_str(path, "path", allow_types=(Path,))
    p = _resolve_for_cwd(path)
    return inner(str(p))


ava.extend.wrap("files.delete", _wrapped_delete)


# ── glob ──
def _wrapped_glob(inner: Callable[..., list[Path]], pattern: str = "*") -> list[Path]:
    pattern = coerce_str(pattern, "pattern")
    # glob is not a simple path — the pattern may contain ** / * wildcards.
    # Resolve cwd base first, concatenate the pattern (preserving wildcard
    # semantics), then call the wrapped glob with the normalized pattern.
    if ava.state is None:
        return inner(pattern)
    p_pattern = Path(pattern).expanduser()
    if p_pattern.is_absolute():
        return inner(pattern)
    cwd = _code_namespace.get()
    # Concatenate cwd + pattern, preserve wildcards — resolve() would treat
    # ** as a real filename and break glob semantics, so use literal `cwd /
    # pattern` concatenation without resolve().
    full_pattern = str(cwd / p_pattern)
    return inner(full_pattern)


ava.extend.wrap("files.glob", _wrapped_glob)


# Deliberately no `ava.files.__doc__` override: the SDK core's claim —
# "Relative paths resolve to your workspace folder" — is the single source of
# truth for path resolution (user ruling 2026-08-01, memory-leak audit #577).
# This plugin's cwd tracking is a runtime layer on top of that default, not a
# contract the rendered SDK should state.


# ── wrap ava.understand ───────────────────────────────────────────────────
# Same shape as the ava.files wraps, applied per target: each target's `paths`
# entries are resolved against the tracked cwd during a turn (workspace /
# pre-identity HOME otherwise, via _resolve_for_cwd), then handed to `inner`
# (the wrapped understand) as absolute paths. A `text` target has no paths to
# resolve and passes through untouched. `effort` mirrors the core signature
# (default "max", forwarded verbatim — the core validates it). `UnderstandError`
# rides on the function object; `ava.extend.wrap` carries it (and the
# docstring) forward, so `ava.understand.UnderstandError` survives the wrap.
def _wrapped_understand(
    inner: Callable[..., list[str]],
    targets: list[dict[str, str | list[str]]],
    effort: str = "max",
    max_concurrent: int | None = None,
) -> list[str]:
    # Anything malformed passes through untouched so the wrapped function's
    # canonical TypeError / ValueError fires before any path resolution — that
    # includes a target carrying both `paths` and `text`, or a `paths` value
    # that is not a list of path strings.
    if not isinstance(targets, list):
        return inner(targets, effort=effort, max_concurrent=max_concurrent)
    resolved: list[dict[str, str | list[str]]] = []
    for t in targets:
        if not isinstance(t, dict) or "paths" not in t or "text" in t:
            resolved.append(t)
            continue
        paths = t["paths"]
        if not isinstance(paths, list) or not all(isinstance(p, (str, Path)) for p in paths):
            # Malformed paths value — leave for the core's canonical TypeError.
            resolved.append(t)
            continue
        resolved.append({**t, "paths": [str(_resolve_for_cwd(p)) for p in paths]})
    return inner(resolved, effort=effort, max_concurrent=max_concurrent)


ava.extend.wrap("understand", _wrapped_understand)


# ── wrap ava.ui.serve ────────────────────────────────────────────────────
# serve(dir) registers the served directory with the platform; a relative
# dir must resolve against the plugin-tracked cwd like every other SDK path
# (files / shell / understand), not the agent process cwd — otherwise the
# page-server daemon reports serve_dir missing and the page degrades
# (Task #1523).
def _wrapped_serve(
    inner: Callable[..., Any],
    dir: str,
    name: str,
    port: int | None = None,
    title: str | None = None,
    *,
    ttl: float | None = None,
) -> Any:
    dir = coerce_str(dir, "dir", allow_types=(Path,))
    p = _resolve_for_cwd(dir)
    return inner(str(p), name, port=port, title=title, ttl=ttl)


ava.extend.wrap("ui.serve", _wrapped_serve)


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

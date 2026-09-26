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

This module is the plugin's SDK **surface** — the only face an agent-launched
child loads (task #3633). Its agent-runtime registrations (the `ava_code__cwd`
state field, the two system-prompt sections, the after_init / after_exec
hooks) live in `agent_runtime.py`, imported only on the full path (see
`agent/_extensions.py`); until it loads, `state_handle` below is a stand-in
that raises the same `PluginStateOutsideTurnError` the real handle raises
outside an exec turn.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

__description__ = "Ava Code conventions — maintains cwd and auto-injects project AGENTS.md / CLAUDE.md (walking up from ava.files.read paths)"

import contextlib
from collections.abc import Callable
from pathlib import Path

import ava
import ava.files as _ava_files_mod
import ava.skills as _ava_skills
from ava.sdk_validation import coerce_str
from shared.config import settings
from shared.log import logger

from . import _code_namespace
from ._walk import find_context_files_along_path, project_skill_roots

_files_resolve = _ava_files_mod._resolve

# ── ava.cwd SDK namespace registration — runs before the agent-runtime face's
# register_plugin_state (loaded right after this surface), so a plugin
# double-load (test fixture / dev hot-reload) hits the namespace conflict first
# (`PluginNamespaceConflictError`, PR #192's first line of defense) rather than
# the state-field reducer-function-identity-mismatch annotation conflict
# (function objects differ after reload).
# _code_namespace's top level does not depend on state_handle (function
# bodies lazy-import), so importing here is cycle-free.
ava.register_namespace("cwd", _code_namespace)
# Promote cwd into the system prompt's expanded SDK reference, ahead of the
# configured framework list — it is the top coding surface, and a framework
# default cannot name a plugin namespace (issue #1011).
ava.register_sdk_expand("cwd")


# ── state handle (surface stand-in) ──────────────────────────────────────
# The real handle — and the state class it belongs to — lives in the
# agent-runtime face (`agent_runtime.py`), loaded when a state slot first
# materializes (task #3633 leg-2: a stateful child's slot is lazy). This
# stand-in exists so the surface's call sites (`ava.cwd.get`/`set`, the read
# wrap's injection path, the project-skill source below) always hold a handle
# object. With a live slot (`ava.state` is not None), the slot exposes
# `materialize()` — the framework contract on the lazy slot: calling it loads
# the face (which rebinds `state_handle` on this module) and the call then
# delegates to the real handle. The rebind replaces the module attribute only,
# so a holder that bound the stand-in before it (a call-site local, or the name
# read once and reused across an update pair) must delegate forward: `_forward`
# re-reads the module first and hands over once the binding has moved on (task
# #3665). Without a live slot — outside a turn (test / dev REPL) — its methods
# raise exactly what the real handle raises outside an exec turn.
class _UnboundStateHandle:
    """Stand-in for the ava_code `PluginStateHandle` until the slot resolves.

    Once the slot materializes the real handle is rebound onto this module;
    a holder still pointing here delegates forward on its next call — see
    `_forward`."""

    def read(self) -> Any:
        return self._forward(
            "read",
            "PluginStateHandle[AvaCodeState].read() called outside exec turn—"
            "ava.state only valid inside execute_code (the exec turn).",
        )

    def update(self, delta: dict[str, Any]) -> None:
        self._forward(
            "update",
            "PluginStateHandle[AvaCodeState].update() called outside exec turn—"
            "ava.state_update only valid inside execute_code (the exec turn).",
            delta,
        )

    def _forward(self, method: str, outside_turn_message: str, *args: Any) -> Any:
        """Delegate to the module's current handle; materialize a live slot first."""
        # A stale holder (bound before the slot materialized — e.g. the name
        # read once and reused across `ava.cwd.set`'s cwd + cwd_note pair)
        # must delegate before reading `ava.state`: once materialized that is
        # the real state, which has no `materialize()` — the stale path would
        # otherwise fall through to the outside-turn raise after having
        # already succeeded once (#3665).
        from .plugin import state_handle as current

        if current is not self:
            return getattr(current, method)(*args)
        state = ava.state
        if state is not None:
            materialize = getattr(state, "materialize", None)
            if materialize is not None:
                materialize()
                from .plugin import state_handle as current

                if current is not self:
                    return getattr(current, method)(*args)
        raise ava.PluginStateOutsideTurnError(outside_turn_message)


state_handle: _UnboundStateHandle = _UnboundStateHandle()


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
    # Agent-runtime half only: reachable when a live state slot exists
    # (a stateful child / the agent process), so these imports stay off the
    # surface boot (task #3633).
    from agent.graph._exec_output import truncate_both_ends
    from agent.messages import NoteTag, system_note_message
    from ava.security import scan_content

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

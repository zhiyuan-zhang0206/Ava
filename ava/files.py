"""Relative paths resolve to your workspace folder; `~/...` resolves to your home directory."""

__all_for_ava__ = [
    "append",
    "delete",
    "edit",
    "glob",
    "read",
    "write",
]

import difflib
import glob as _glob
import os
from pathlib import Path

from ava import _boot
from ava._sdk_validation import coerce_str, coerce_typed
from ava.security import is_flagged, scan_content
from shared.log import logger
from shared.paths import ava_home, workspace_dir

# Module-load invariant assert: if `Path.home()` is unavailable
# (container / sandbox without $HOME returns Path("") or a nonexistent
# path), blow up at import time, avoiding silent runtime re-fallback to
# process cwd — which would break this module's core contract.
#
# Don't capture `_HOME` at module level: per-call `Path.home()` lets
# runtime $HOME env changes (test fixture / `ava.user`-style isolation)
# take effect immediately; the per-call cost of `os.environ.get("HOME")`
# is negligible.
_home_at_load = Path.home()
assert _home_at_load.is_absolute() and _home_at_load.is_dir(), (  # noqa: S101
    f"$HOME unavailable at module load (Path.home() = {_home_at_load!r})"
)
del _home_at_load


def _resolve(path: str | Path) -> Path:
    """Resolve a path: expand `~`, then prepend the agent's workspace
    (`$HOME` before a process identity is bound) if still relative."""
    # `expanduser` translates `~` / `~user`; if still a relative path,
    # prepend the per-agent workspace. The workspace is a framework
    # concept, so it lives here in the SDK core — plugins may layer cwd
    # *tracking* on top, but the no-plugin baseline must not silently
    # fall back to `$HOME` (issue #1008). Before `ava._boot.establish`
    # binds an identity (test / dev REPL without a bootstrap) there is
    # no workspace; `Path.home()` is the documented pre-bootstrap base
    # (per-call live, so test fixture mock env takes effect immediately).
    p = Path(path).expanduser()
    if not p.is_absolute():
        aid = _boot.agent_id()
        # agent id is typed int but is None until a bootstrap establishes it.
        base = workspace_dir(aid) if aid is not None else Path.home()  # pyright: ignore[reportUnnecessaryComparison]
        p = base / p
    return p


def _record_skill_read(path: Path) -> None:
    """Best-effort `skill_invoked` attribution for a file that is a loaded
    skill's SKILL.md (the `.path` + `files.read(SKILL.md)` pattern).

    Reading a skill body through the generic file API is consumption exactly
    like a `help()` render, but it bypasses the lazy proxy loader that carries
    the attribution hook — so it used to go unrecorded and the `loaded` signal
    was under-counted. The resolution happens in the skills module (only a
    mounted skill's own SKILL.md qualifies); a non-matching file is a no-op.

    Telemetry, never a failure path: any problem (the skills surface disabled
    by `AVA_SDK_DISABLE`, an import hiccup, a scan error) is swallowed with a
    warning — a file read must never break because attribution could not run."""
    if path.name != "SKILL.md":
        return
    try:
        from ava import skills as skills_mod

        skills_mod._record_skill_invoked_by_path(path)
    except Exception as e:  # best-effort telemetry; never break a read
        logger.warning("skill attribution skipped for {}: {}", path, e)


def read(
    path: str | Path,
    start: int | None = None,
    end: int | None = None,
    *,
    limit: int | None = None,
    with_line_numbers: bool = False,
) -> str:
    """Read a file, or a 1-indexed inclusive line range.

    `limit` (max lines from `start`) is mutually exclusive with `end`.
    """
    path = coerce_str(path, "path", allow_types=(os.PathLike,))
    start = coerce_typed(start, "start", int, allow_none=True)
    end = coerce_typed(end, "end", int, allow_none=True)
    limit = coerce_typed(limit, "limit", int, allow_none=True)
    with_line_numbers = coerce_typed(with_line_numbers, "with_line_numbers", bool)
    if limit is not None:
        if end is not None:
            raise ValueError("pass either `end` or `limit`, not both")
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        end = (start or 1) + limit - 1

    # A read is an ingestion surface: file bytes flow straight into the agent's
    # context, so the returned text is scanned for injection patterns before it
    # leaves. Clean content is returned byte-for-byte (scan_content is a no-op).
    source = f"file.read:{path}"
    p = _resolve(path)
    # A SKILL.md body entering the context is skill consumption — attribute
    # it even though the generic file API bypasses the lazy proxy hook.
    _record_skill_read(p)
    text = p.read_text(encoding="utf-8")
    if start is None and end is None and not with_line_numbers:
        return scan_content(text, source=source)
    if start is not None and start < 1:
        raise ValueError(f"start must be >= 1, got {start}")
    if end is not None and end < 1:
        raise ValueError(f"end must be >= 1, got {end}")
    if start is not None and end is not None and start > end:
        raise ValueError(f"start ({start}) must be <= end ({end})")
    lines = text.splitlines(keepends=True)
    lo = (start - 1) if start is not None else 0
    hi = end if end is not None else len(lines)
    selected = lines[lo:hi]
    if with_line_numbers:
        last_no = lo + len(selected)
        width = max(1, len(str(last_no)))
        numbered = "".join(f"{lo + i + 1:>{width}}: {line}" for i, line in enumerate(selected))
        return scan_content(numbered, source=source)
    return scan_content("".join(selected), source=source)


def _is_memory_note(p: Path) -> bool:
    """True when `p` resolves to a path under the shared memory pool root.

    A note there is durable and auto-recalled into future sessions, so
    injection carried into it is the persistent-injection vector — worth
    flagging at write time (see `_flag_frontmatter`)."""
    try:
        p.resolve().relative_to((ava_home() / "memory").resolve())
    except ValueError:
        return False
    return True


def _flag_frontmatter(text: str) -> str:
    """Ensure the note's YAML frontmatter carries `injection-risk: flagged`,
    adding a frontmatter block when the note has none. A note that already
    declares the field (any value) is left untouched."""
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            block = text[4:end]
            if "injection-risk:" in block:
                return text
            return f"---\n{block}\ninjection-risk: flagged\n---{text[end + 4 :]}"
    return f"---\ninjection-risk: flagged\n---\n{text}"


def write(path: str | Path, content: str) -> None:
    """Write, creating parent directories if needed."""
    path = coerce_str(path, "path", allow_types=(os.PathLike,))
    content = coerce_str(content, "content")
    p = _resolve(path)
    if _is_memory_note(p) and is_flagged(content):
        content = _flag_frontmatter(content)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def append(path: str | Path, content: str) -> None:
    """Append to a file, creating it (and parent directories) if absent."""
    path = coerce_str(path, "path", allow_types=(os.PathLike,))
    content = coerce_str(content, "content")
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Appending injection-flagged content to a memory note taints the whole
    # note; rewrite it so the note's frontmatter carries the flag (a pure
    # append could not touch the frontmatter, which sits at the top).
    if _is_memory_note(p) and is_flagged(content):
        prior = p.read_text(encoding="utf-8") if p.exists() else ""
        p.write_text(_flag_frontmatter(prior + content), encoding="utf-8")
        return
    with p.open("a", encoding="utf-8") as f:
        f.write(content)


_FUZZY_HINT_THRESHOLD = 0.4


def _closest_match_hint(content: str, old: str) -> str:
    """When `old` isn't in `content` verbatim, locate the most similar
    contiguous line range and return a one-block unified-diff hint.

    Returns the empty string when no slice is close enough (similarity
    below `_FUZZY_HINT_THRESHOLD`) or when inputs are degenerate (`old`
    empty / `content` empty / `old` longer than `content`).
    """
    content_lines = content.splitlines(keepends=True)
    old_lines = old.splitlines(keepends=True)
    if not old_lines or not content_lines or len(old_lines) > len(content_lines):
        return ""

    window_size = len(old_lines)
    best_ratio = 0.0
    best_start = -1
    matcher = difflib.SequenceMatcher(a="", b=old, autojunk=False)
    for i in range(len(content_lines) - window_size + 1):
        window = "".join(content_lines[i : i + window_size])
        matcher.set_seq1(window)
        # `quick_ratio` is an upper bound; skip the expensive call when
        # we can't possibly beat the running best.
        if matcher.quick_ratio() < best_ratio:
            continue
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = i

    if best_start < 0 or best_ratio < _FUZZY_HINT_THRESHOLD:
        return ""

    s = best_start + 1  # 1-indexed inclusive
    e = best_start + window_size
    window_text = "".join(content_lines[best_start : best_start + window_size])
    diff = "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            window_text.splitlines(),
            fromfile="your `old`",
            tofile=f"actual lines {s}-{e}",
            lineterm="",
            n=3,
        )
    )
    return f"\n\nClosest match at lines {s}-{e} ({int(best_ratio * 100)}% similar):\n{diff}"


def edit(path: str | Path, old: str, new: str, *, replace_all: bool = False) -> None:
    """`old` must appear exactly once unless `replace_all=True`. When it is
    not found, the error includes a diff against the closest match in the
    file.
    """
    path = coerce_str(path, "path", allow_types=(os.PathLike,))
    old = coerce_str(old, "old")
    new = coerce_str(new, "new")
    replace_all = coerce_typed(replace_all, "replace_all", bool)
    p = _resolve(path)
    content = p.read_text(encoding="utf-8")
    count = content.count(old)
    if count == 0:
        hint = _closest_match_hint(content, old)
        raise ValueError(f"old not found in {path!r}{hint}")
    if count > 1 and not replace_all:
        raise ValueError(
            f"old appears {count} times in {path!r}; pass replace_all=True to replace all"
        )
    new_content = content.replace(old, new) if replace_all else content.replace(old, new, 1)
    p.write_text(new_content, encoding="utf-8")


def glob(pattern: str = "*") -> list[Path]:
    pattern = coerce_str(pattern, "pattern")
    p = _resolve(pattern)
    # Path.glob in Python 3.12 doesn't accept an absolute pattern, and
    # `**` spanning multiple levels is cleaner via stdlib
    # `glob.glob(recursive=True)` than pathlib. Can refactor on 3.13+.
    return sorted(Path(s) for s in _glob.glob(str(p), recursive=True))  # noqa: PTH207


def delete(path: str | Path) -> None:
    """Delete a file (not a directory)."""
    path = coerce_str(path, "path", allow_types=(os.PathLike,))
    _resolve(path).unlink()

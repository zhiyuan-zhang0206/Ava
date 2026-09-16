"""Your persistent logical working directory; starts at your workspace."""

# Audience = agent. Dev-perspective implementation details (state_handle /
# LangGraph reducer / deepcopy isolation, etc.) live in the top-of-file
# comment in plugins/ava_code/plugin.py.
from pathlib import Path

from loguru import logger

__all_for_ava__ = ["get", "set"]


def get() -> Path:
    # Hold no local name for the handle: until the lazy state slot materializes
    # the module holds the surface stand-in, and materialization rebinds the
    # MODULE attribute — a stale local keeps calling the stand-in after
    # `ava.state` has become the real state (task #3665). The lazy import keeps
    # `from . import _code_namespace` cycle-free (the binding happens after
    # register_namespace in plugin.py).
    from . import plugin as _plugin

    return Path(_plugin.state_handle.read().cwd)


def set(path: str | Path) -> None:
    """Persistent across turns and restarts.

    AvaCode SDK wrappers resolve relative paths against this value. The
    Python process working directory is unchanged.

    Args:
        path: relative paths resolve against the current logical directory;
            `~/...` is expanded.
    """
    import stat as _stat

    import ava.skills as _ava_skills

    from . import plugin as _plugin
    from ._walk import project_skill_roots

    p = Path(path).expanduser()
    # Single stat avoids the TOCTOU race between exists() → is_dir() syscalls.
    p = (get() / p).resolve() if not p.is_absolute() else p.resolve()
    try:
        st = p.stat()
    except FileNotFoundError as e:
        raise FileNotFoundError(f"ava.cwd.set: path does not exist: {p}") from e
    if not _stat.S_ISDIR(st.st_mode):
        raise NotADirectoryError(f"ava.cwd.set: path is not a directory: {p}")
    # Re-read the module attribute at each use: the first update may
    # materialize the lazy slot and rebind `plugin.state_handle`, so a name
    # bound up front would go stale on the updates below (task #3665).
    _plugin.state_handle.update({"cwd": str(p)})

    # Set a cwd-note for the after-exec hook to inject as a system note.
    # Same-turn dedup: each call overwrites cwd_note; only the final
    # value is injected.  Project skills (if any) go through the separate
    # project_skills_note mechanism so they survive across compactions.
    _plugin.state_handle.update({"cwd_note": f"Working directory set to {p}"})
    try:
        loaded = _ava_skills.skills_in(project_skill_roots(p))
    except AttributeError:
        # ava.skills is disabled via AVA_SDK_DISABLE — project-local skill
        # listing is unavailable; cwd change itself succeeds normally.
        logger.debug("ava.skills unavailable under AVA_SDK_DISABLE")
    else:
        if loaded:
            lines: list[str] = []
            for s in loaded:
                ident = _ava_skills.identifier(s)
                target = _ava_skills.target(s)
                desc = s["description"]
                path_str = s["path"]
                lines.append(f"  - {ident} (ava.skills.{target}) — {desc}")
                lines.append(f"      {path_str}")
            summary = f"Skills available in this repo ({len(loaded)}):\n" + "\n".join(lines)
            _plugin.state_handle.update({"project_skills_note": summary})
        else:
            _plugin.state_handle.update({"project_skills_note": None})

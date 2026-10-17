"""The home directory a process acts on, and which checkout may operate it.

Pure `os.environ` and `Path` reads, so everything that needs `$MEIVA_HOME` (the `.env`
boot, the path helpers, the CLI's pre-Settings gate) can import it without
Settings, a log sink or a database.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["default_home", "home_checkout_error", "is_default_home", "resolve_meiva_home"]


def resolve_meiva_home() -> Path:
    """This process's home: `$MEIVA_HOME` when set, else `~/.meiva`.

    Read on every call — never cached — so a caller that changes the variable
    (a test session, a tool that sets it once at its top) is followed by every
    later call. The path is neither created nor checked here
    (`base.paths.meiva_home` creates it).

    Raises:
        ValueError: `$MEIVA_HOME` is not an absolute path; a relative home would
            follow the working directory.
    """
    env = os.environ.get("MEIVA_HOME")
    if not env:
        return Path.home() / ".meiva"
    home = Path(env).expanduser()
    if not home.is_absolute():
        raise ValueError(f"MEIVA_HOME must be an absolute path, got {env!r}")
    return home


def home_checkout_error(repo: Path) -> str | None:
    """Why `repo` may not operate this process's home, or None when it may.

    A home that carries its own `<home>/source` checkout (the production home
    `~/.meiva`, and every unit started from source) is operated only by that
    checkout's code: a disposable checkout driving it would bind its daemons to
    code that disappears with the checkout (the 01:13 worktree accident, Task
    #966) or apply un-reviewed migrations to its database. A home with no
    `source` (a test session's temporary home, a scratch `MEIVA_HOME`) accepts any
    checkout.

    Both sides are compared resolved, so a symlinked or `..`-spelled path to
    the same directory is the same checkout.
    """
    home = resolve_meiva_home()
    source = home / "source"
    if not source.is_dir() or repo.resolve() == source.resolve():
        return None
    return (
        f"this checkout ({repo}) is not the checkout of the home it would act on: "
        f"{home} carries its own source checkout ({source}), and only that "
        f"checkout may operate it. Run {source}/.venv/bin/meiva (the bare `meiva` of a "
        f"production host) instead, or set MEIVA_HOME to a home of your own (a "
        f"temporary directory, for a test or a tool)."
    )


def default_home() -> Path:
    """The default (prod) cluster home, `~/.meiva` — the one home whose initialization
    uses the fixed default port block and whose checkout anchors without a
    pointer."""
    return Path.home() / ".meiva"


def is_default_home(home: Path) -> bool:
    """Whether `home` is the default prod home (`~/.meiva`)."""
    return Path(home).expanduser().resolve() == default_home().resolve()

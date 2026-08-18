"""Profile provisioning for the shared headed Chrome (ava-browser).

The browser daemon runs Chrome against a *dedicated* profile at
`$AVA_HOME/chrome-profile`, isolated from the operator's daily Chrome. On a
fresh install that profile is empty, so the agent must sign in to every site
from scratch. This module offers the alternative at first-start time: seed the
dedicated profile by COPYING the operator's daily Chrome profile, so the agent
inherits every logged-in session and acts as the user without re-authenticating.

Copying hands the agent the user's full logged-in identity (cookies, sessions,
saved passwords, signed-in accounts), so it is a SECURITY decision. It is made
only interactively and only when the dedicated profile does not yet exist — an
already-populated profile is NEVER touched (constraint: prod carries a multi-GB
logged-in profile that must survive every `ava start`).

`ensure_browser_profile` is called from the converge `_ensure_browser` step,
which runs at `ava start` before the browser session launches. Non-interactive
paths (watchdog respawn, boot autostart, `ava cluster update` rollout) pass
`interactive=False` and take the fresh-profile default without prompting.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from shared.config import settings
from shared.platform_probes import default_chrome_user_data_dir
from shared.proc import process_alive

# Names never worth copying out of a Chrome user-data dir. `Singleton*` are the
# live lock/socket/cookie files Chrome holds while running — copying them would
# import a stale lock that confuses the agent's Chrome. The rest are large,
# regenerable caches (GPU/shader/HTTP/service-worker) that only bloat the copy;
# Chrome rebuilds them on demand. Everything else — cookies, Login Data, Local
# State, per-profile prefs — is copied so the agent inherits the logged-in state.
_EXCLUDED_NAMES = frozenset(
    {
        "Cache",
        "Code Cache",
        "GPUCache",
        "GrShaderCache",
        "ShaderCache",
        "CacheStorage",
        "ScriptCache",
        "Crashpad",
        "component_crx_cache",
    }
)


def profile_dir() -> Path:
    """The agent's dedicated Chrome profile (`$AVA_HOME/chrome-profile`).

    Single source of truth shared by the daemon (which launches Chrome against
    it) and this module (which may seed it)."""
    return Path(settings.general.ava_home).expanduser() / "chrome-profile"


def profile_is_populated(path: Path) -> bool:
    """True when `path` is a non-empty directory — an existing profile to leave
    untouched. A missing or empty dir counts as fresh (eligible for seeding)."""
    return path.is_dir() and any(path.iterdir())


def _ignore_junk(_src: str, names: list[str]) -> set[str]:
    """`shutil.copytree` ignore callback: drop lock/socket files and regenerable
    caches (see `_EXCLUDED_NAMES`)."""
    return {n for n in names if n.startswith("Singleton") or n in _EXCLUDED_NAMES}


def _dir_size_bytes(src: Path) -> int:
    """Size (bytes) of what a copy would carry: walk `src` applying the same
    exclusions `_copy_default_profile` uses, so the reported figure matches the
    bytes actually copied. Symlinks are not followed (their target isn't copied)."""
    total = 0
    for root, dirs, files in os.walk(src):
        ignored = _ignore_junk(root, dirs + files)
        dirs[:] = [d for d in dirs if d not in ignored]  # prune -> os.walk won't descend
        for name in files:
            if name in ignored:
                continue
            try:
                total += (Path(root) / name).stat(follow_symlinks=False).st_size
            except OSError:
                continue  # vanished/unreadable mid-walk — not worth failing a size estimate
    return total


def _running_chrome_pid(src: Path) -> int | None:
    """Pid of a Chrome holding `src` open, or None if none is.

    Chrome writes a `SingletonLock` symlink -> `<hostname>-<pid>` while a
    user-data dir is open. A live pid means the SQLite stores (Cookies, Login
    Data) may be mid-write, so copying now risks a corrupt import — the caller
    refuses until Chrome is quit. A stale lock (process gone after a crash) or a
    non-symlink/missing lock reads as 'not running'. The pid is trusted over a
    hostname match: a false 'running' (harmless retry prompt) is preferable to a
    false 'not running' (a corrupt copy)."""
    try:
        target = (src / "SingletonLock").readlink()
    except OSError:
        return None  # no lock, or not a symlink -> not running
    pid_str = str(target).rpartition("-")[2]
    if not pid_str.isdigit():
        return None
    pid = int(pid_str)
    # `process_alive`, not a raw `os.kill(pid, 0)`: on Windows signal 0 is not a
    # valid signal and `os.kill(pid, 0)` maps to TerminateProcess — a liveness
    # probe must never kill the operator's daily Chrome. `process_alive` answers
    # the same question via psutil there, and a recycled pid owned by another
    # user still counts as alive (running).
    if process_alive(pid):
        return pid
    return None  # stale lock


def _copy_default_profile(src: Path, dst: Path) -> None:
    """Copy the daily Chrome user-data dir `src` into the agent profile `dst`,
    excluding locks + caches. `dst` must be absent or empty (the caller guarantees
    it is not an existing profile). On any failure the partial `dst` is removed so
    a half-copied profile is never mistaken for a real one."""
    if dst.exists():
        shutil.rmtree(dst)
    try:
        # symlinks=True copies links verbatim rather than following them, so a
        # broken link in the source can't abort the whole copy.
        shutil.copytree(src, dst, ignore=_ignore_junk, symlinks=True)
    except BaseException:
        shutil.rmtree(dst, ignore_errors=True)
        raise


def _human_size(n_bytes: int) -> str:
    gb = n_bytes / 1024**3
    return f"{gb:.1f} GB" if gb >= 1 else f"{n_bytes / 1024**2:.0f} MB"


def _prompt_profile_choice(src: Path) -> str:
    """Explain the choice and return 'copy' or 'new' (default 'new')."""
    print("\n  The agent's browser uses a DEDICATED Chrome profile, separate from your")  # noqa: T201
    print("  daily Chrome. Fresh, it is empty — the agent signs in to every site itself.")  # noqa: T201
    print("")  # noqa: T201
    print(f"  You can instead COPY your daily Chrome profile ({src}) into it. The agent")  # noqa: T201
    print("  then inherits every logged-in session and acts on any site you are signed")  # noqa: T201
    print("  into — using your cookies, saved passwords, and accounts — without a re-login.")  # noqa: T201
    print("")  # noqa: T201
    print("  SECURITY: a copied profile gives the agent your full logged-in identity;")  # noqa: T201
    print("  anything it does in the browser is done as you.")  # noqa: T201
    answer = input(
        "  Seed the browser profile from your daily Chrome? [copy / new] (default new): "
    )
    return "copy" if answer.strip().lower() in ("copy", "c") else "new"


def _confirm_copy(src: Path, dst: Path, size_bytes: int) -> bool:
    """Final size-aware confirmation for the copy. Returns True only on explicit yes."""
    print(f"\n  This copies {_human_size(size_bytes)} from")  # noqa: T201
    print(f"    {src}")  # noqa: T201
    print("  into the agent's profile at")  # noqa: T201
    print(f"    {dst}")  # noqa: T201
    print("  The agent will then be logged into everything you are logged into.")  # noqa: T201
    return input("  Type 'yes' to confirm the copy: ").strip().lower() in ("yes", "y")


def ensure_browser_profile(*, interactive: bool) -> None:
    """Offer, once, to seed the agent's dedicated Chrome profile from the daily one.

    No-ops (leaving the daemon to create a fresh empty profile) when the profile
    already exists non-empty, when not interactive, or when this host has no daily
    Chrome profile to copy from. Otherwise prompts; on 'copy' it refuses while
    Chrome is still running (retry loop), reports the size, takes a final
    confirmation, then copies."""
    dst = profile_dir()
    if profile_is_populated(dst):
        return  # never clobber an existing profile
    if not interactive:
        return  # non-interactive default = fresh empty profile
    src = default_chrome_user_data_dir()
    if src is None:
        return  # no daily Chrome to copy from -> fresh profile, no prompt

    if _prompt_profile_choice(src) != "copy":
        return

    # Copying a live profile risks importing half-written SQLite. Require Chrome
    # closed; loop so the operator can quit it and continue without restarting.
    while _running_chrome_pid(src) is not None:
        print("\n  Chrome is still running — copying now could corrupt the imported data.")  # noqa: T201
        if (
            input("  Quit Chrome fully, then press Enter to retry (or type 'new'): ")
            .strip()
            .lower()
            == "new"
        ):
            return

    if not _confirm_copy(src, dst, _dir_size_bytes(src)):
        return
    print(f"  Copying your Chrome profile into {dst} … (this may take a while)")  # noqa: T201
    _copy_default_profile(src, dst)
    print("  Done — the agent's browser now shares your logged-in Chrome state.")  # noqa: T201

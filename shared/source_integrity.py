"""Installed-commit bookmark — the commit that was last fully installed.

A single file (`$AVA_HOME/installed_sha`) recording the git commit that was
last `uv sync`'d and for which migrations were applied. Written by
`ava cluster update` after a successful checkout + sync + migrate, and by `ava start`
right after it applies pending migrations on the current code.

The integrity guard at `ava start` compares HEAD against this bookmark:
- Equal: nothing changed since last install — safe to start.
- Different: someone changed the source tree (manual git pull / reset /
  checkout) without going through the update flow. The start path auto-heals
  by running `uv sync` before bringing services up, so the venv matches HEAD.
"""

from __future__ import annotations

from shared.paths import installed_sha_path


def get() -> str | None:
    """The commit this host last fully installed, or None if never recorded."""
    path = installed_sha_path()
    try:
        content = path.read_text().strip()
    except FileNotFoundError:
        return None
    if not content:
        return None
    return content


def set_installed(sha: str) -> None:
    """Record `sha` as the commit this host has now fully installed."""
    path = installed_sha_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sha.strip() + "\n")


def clear() -> None:
    """Remove the bookmark (recovery path — forces re-seed on next start)."""
    installed_sha_path().unlink(missing_ok=True)

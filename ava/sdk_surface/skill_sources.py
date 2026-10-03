"""Plugin-contributed skill-root providers — framework-internal registry.

A plugin declares (`PluginContributions.skill_sources`) a callable returning skill-root directories
computed at scan time (used for project-local skills whose location depends on runtime cwd).
`ava.sdk_surface.install` adds them and undoes them on reload; the scanner (`ava.skills`) reads the
installed roots.

The list lives here, off the agent-facing `ava.skills` module, so the installer
can write it and the scanner can read it without either reaching through
`ava.skills` — which `AVA_SDK_DISABLE` may replace with a stub (a hermetic
bench that scopes the skills surface out). Per-process state: each agent is its
own process.

This module is framework-internal: not agent-facing, never in the
`ava.help()` view.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

_PROVIDERS: list[Callable[[], list[Path]]] = []


def add(provider: Callable[[], list[Path]]) -> Callable[[], None]:
    """Append a skill-root provider; returns the undo. Called by `ava.sdk_surface.install` for a
    plugin's declared `skill_sources`."""
    _PROVIDERS.append(provider)

    def undo() -> None:
        _PROVIDERS.remove(provider)

    return undo


@contextmanager
def scoped(provider: Callable[[], list[Path]]) -> Generator[None]:
    """Hold `provider` for the duration of a `with` block and take back only that one.

    For a framework caller that needs a request-scoped skill root (the ops runner's per-agent
    command view): the installed plugin providers stay in place, and the scoped one never outlives
    the call even when it raises.
    """
    undo = add(provider)
    try:
        yield
    finally:
        undo()


def roots() -> list[Path]:
    """Flatten every registered provider's roots into one list (scan order)."""
    out: list[Path] = []
    for provider in _PROVIDERS:
        out.extend(provider())
    return out

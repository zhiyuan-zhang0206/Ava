"""Plugin-contributed skill-root providers — framework-internal registry.

A plugin declares (`PluginContributions.skill_sources`) a callable returning skill-root directories
computed at scan time (used for project-local skills whose location depends on runtime cwd).
`ava.sdk_surface.install` collects them into the `Installation`; the scanner (`ava.skills`) reads the
installed roots. A framework caller can also hold one extra provider for the duration of a call
(`scoped` — the ops runner's per-agent command view): that builds a new installation value with the
provider appended and takes it back afterwards.

The list lives off the agent-facing `ava.skills` module, so the installer can write it and the scanner
can read it without either reaching through `ava.skills` — which `AVA_SDK_DISABLE` may replace with a
stub (a hermetic bench that scopes the skills surface out). Per-process state: each agent is its
own process.

This module is framework-internal: not agent-facing, never in the
`ava.help()` view.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

_SkillProvider = Callable[[], list[Path]]


def add(provider: _SkillProvider) -> Callable[[], None]:
    """Mount `provider` on this process's skill-root providers; returns the undo.

    The install collects a plugin's declared `skill_sources` into the installation it
    is building; this mounts one on the CURRENT value instead (tests and the `scoped`
    context manager). The undo removes exactly that provider."""
    from . import install as _install

    return _install.mount_skill_provider(provider)


@contextmanager
def scoped(provider: _SkillProvider) -> Generator[None]:
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
    from . import install as _install

    current = _install.installed()
    if current is None:
        return []
    out: list[Path] = []
    for provider in current.skill_providers:
        out.extend(provider())
    return out

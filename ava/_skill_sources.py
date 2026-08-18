"""Plugin-contributed skill-root providers — framework-internal registry.

A plugin registers a callable returning skill-root directories computed at scan
time (used for project-local skills whose location depends on runtime cwd). The
agent-facing `ava.skills.register_skill_source` is the registration entry point;
the scanner (`ava.skills`) reads the registered roots; the kernel's plugin
reload (`agent.state.clear_plugin_registrations`) clears them between reloads.

The registry lives here, off the agent-facing `ava.skills` module, so the kernel
can clear it and the scanner can read it without either reaching through
`ava.skills` — which `AVA_SDK_DISABLE` may replace with a stub (a hermetic
bench that scopes the skills surface out). Per-process state: each agent is its
own process.

This module is framework-internal (underscore-prefixed): not agent-facing, never
in the `ava.help()` view.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

_PROVIDERS: list[Callable[[], list[Path]]] = []


def register(provider: Callable[[], list[Path]]) -> None:
    """Append a skill-root provider. Called via `ava.skills.register_skill_source`."""
    _PROVIDERS.append(provider)


def clear() -> None:
    """Drop all registered providers, so the next plugin load re-registers from
    empty state. Called by `agent.state.clear_plugin_registrations`."""
    _PROVIDERS.clear()


def roots() -> list[Path]:
    """Flatten every registered provider's roots into one list (scan order)."""
    out: list[Path] = []
    for provider in _PROVIDERS:
        out.extend(provider())
    return out

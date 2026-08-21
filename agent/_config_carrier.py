"""Per-agent config map retention — the env payloads the agent process pops
at boot, kept so the exec subprocess can re-emit them into its child's
environment.

`agent/loop.py` pops `AVA_AGENT_CONFIG_OVERLAY` / `AVA_AGENT_BIRTH_CONFIG`
at boot so the agent's own children (shell sessions, watchers) do not
inherit them (issue #974 — either JSON blob may carry a Settings field as
sensitive as a provider api_key, and env inheritance is the leak). The exec
child is the one child that MUST see them: SDK calls made from agent code
(`ava.understand`, `ava.web.fetch`, ...) resolve settings through the same
overlay the agent process booted with, so `agent/graph/_exec.py` re-emits
both maps via `_build_child_env`.

A module-level slot is the whole surface: the maps are process-lifetime
boot constants (a config change replaces the process via restart), so a
plain store/get pair — no registry, no invalidation.
"""

from __future__ import annotations

from types import SimpleNamespace

# Mutable holder — store() rebinds its fields; a plain namespace keeps the
# rebinding free of `global` (the maps are boot constants, see module docstring).
_store = SimpleNamespace(
    config_overlay=None,
    birth_config=None,
)


def store_config_maps(
    config_overlay: dict[str, object] | None,
    birth_config: dict[str, object] | None,
) -> None:
    _store.config_overlay = config_overlay
    _store.birth_config = birth_config


def get_config_maps() -> tuple[dict[str, object] | None, dict[str, object] | None]:
    return _store.config_overlay, _store.birth_config


__all__ = ["get_config_maps", "store_config_maps"]

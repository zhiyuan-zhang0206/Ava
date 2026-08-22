"""Lazy loader for provider plugins — the one place a plugin's ``provider.py``
is imported.

Plugin provider registration must happen in *every* process that builds or
validates a chat model — the agent, the gateway (spawn validation + model
lists), the labeler daemon, the eval harness — none of which loads
``plugin.py`` (that loader is agent-process-only, and layering forbids
``shared`` from importing it). This loader lives in ``shared``, reuses the
existing plugin discovery + enable config, and imports only ``provider.py``
(shared-only dependencies), the same standalone-by-path idiom as
``default_config.py`` (``shared/plugins_config.py:update_all_disk_images``).

Loaded once per process, on the first registry-consulting call
(``build_chat_model`` / ``validate_model_config`` / ``get_models`` / the
gateway's per-model views). Import order is sorted plugin names — deterministic
rather than filesystem-order. A provider.py that fails to import or register
raises out of the triggering call: an enabled plugin whose provider code is
broken fails the process loudly, never silently omits its models.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path

_lock = threading.Lock()


class _LoaderState:
    """Mutable loader state kept off the module global namespace."""

    def __init__(self) -> None:
        self.loaded = False


_STATE = _LoaderState()


def _load_one(name: str, provider_py: Path, *, is_builtin: bool) -> None:
    from shared.lm import provider_api

    pkg = "ava_builtins.plugins" if is_builtin else "plugins"
    spec = importlib.util.spec_from_file_location(f"{pkg}.{name}.provider", provider_py)
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"provider plugin {name!r}: spec_from_file_location returned None "
            f"for existing {provider_py}"
        )
    module = importlib.util.module_from_spec(spec)
    # Register into sys.modules BEFORE exec_module — the same idiom as the
    # plugin.py loader: pydantic models defined inside the module need their
    # module globals reachable for get_type_hints / ForwardRef resolution.
    sys.modules[spec.name] = module
    provider_api._CURRENT_PLUGIN = name
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise RuntimeError(f"provider plugin {name!r} failed to load ({provider_py})") from e
    finally:
        provider_api._CURRENT_PLUGIN = None


def ensure_provider_plugins_loaded() -> None:
    """Import every enabled plugin's ``provider.py``, once per process.

    Idempotent and thread-safe (the gateway serves spawn endpoints from a
    thread pool; two concurrent first calls must not double-register).
    """
    with _lock:
        if _STATE.loaded:
            return
        from shared import paths, plugins_config
        from shared.lm import provider_api
        from shared.lm.factory import _MODEL_KEY_MAP

        # Bootstrap can be the first provider consumer. Importing factory here
        # establishes its core-prefix reservation before any plugin registers;
        # otherwise bootstrap-first startup could let a plugin claim `claude-`.
        provider_api.REGISTRY.reserve_core_prefixes(set(_MODEL_KEY_MAP))

        discovered = plugins_config._discover_plugins()
        known = set(discovered)
        config = plugins_config.load(known)
        repo_dir = str(paths.repo_plugins_dir())
        for name in sorted(config.plugins):
            if not config.plugins[name].enabled:
                continue
            plugin_dir = discovered.get(name)
            if plugin_dir is None:
                continue
            provider_py = plugin_dir / "provider.py"
            if not provider_py.exists():
                continue
            is_builtin = repo_dir in str(plugin_dir.resolve())
            _load_one(name, provider_py, is_builtin=is_builtin)
        _STATE.loaded = True


def _reset_loaded_for_tests() -> None:
    """Clear the once-per-process flag — test support only.

    Tests that exercise the loader against fixture plugin dirs reset between
    cases; no production path calls this.
    """
    with _lock:
        _STATE.loaded = False

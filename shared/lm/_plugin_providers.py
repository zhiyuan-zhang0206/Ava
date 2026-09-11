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
rather than filesystem-order. A provider.py whose module body raises is
contained with a loud report (``shared.plugin_load_report``): the failure is
recorded, the rest of that module is abandoned, and the remaining providers
still load — the fail-soft contract (user ruling 2026-09-11): one broken
plugin's provider code must not take down every process that builds a model.
The half-executed module is dropped from ``sys.modules``, so a later attempt
re-executes the module body from the top — note ``register()`` is not
transactional, so prefixes a failing attempt registered before the raise stay
bound and surface on that retry as a fail-closed registration-contract error
rather than binding twice. One exception, deliberately fail-closed: a
`register()` contract violation (`provider_api.ProviderRegistrationError` —
duplicate/nested prefix, mismatched model, bad price data) propagates, because
the flat prefix and model-id maps cannot pick a winner between two claimants.

Core registers no providers. At least one enabled provider plugin must bind at
load time; an empty registry still raises before the once flag is set (a
configuration failure is not contained), so correcting the enable
configuration can be retried in the same process.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path

from shared import plugin_load_report

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
    except provider_api.ProviderRegistrationError:
        # Fail-closed by design — the loader lets this class propagate instead
        # of containing it (a flat prefix/model map cannot pick a winner).
        # Still drop the half-executed module: a later attempt retries clean.
        sys.modules.pop(spec.name, None)
        raise
    except Exception as e:
        # A code-load failure: drop the half-executed module and wrap for the
        # loader, which contains it loudly (the same cleanup the plugin.py
        # loader's fail-soft contract performs).
        sys.modules.pop(spec.name, None)
        raise RuntimeError(f"provider plugin {name!r} failed to load ({provider_py})") from e
    finally:
        provider_api._CURRENT_PLUGIN = None


def ensure_provider_plugins_loaded() -> None:
    """Import every enabled plugin's ``provider.py``, once per process.

    Idempotent and thread-safe (the gateway serves spawn endpoints from a
    thread pool; two concurrent first calls must not double-register). Core
    contributes no fallback binding, so loading zero providers is a retryable
    startup error.
    """
    with _lock:
        if _STATE.loaded:
            return
        from shared import paths, plugins_config
        from shared.lm import provider_api
        from shared.lm import registry as model_registry
        from shared.lm.factory import _MODEL_KEY_MAP

        # Bootstrap can be the first provider consumer. Importing factory here
        # applies the same core-prefix reservation contract before any plugin
        # registers; the set is empty once every provider is plugin-owned.
        provider_api.REGISTRY.reserve_core_prefixes(set(_MODEL_KEY_MAP))

        discovered = plugins_config._discover_plugins()
        known = set(discovered)
        config = plugins_config.load_for_runtime(known)
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
            try:
                _load_one(name, provider_py, is_builtin=is_builtin)
            except (KeyboardInterrupt, SystemExit):
                raise
            except provider_api.ProviderRegistrationError:
                # Registration-contract violation — fail-closed, not contained
                # (the flat maps cannot pick a winner between two claimants).
                # Skip+loud is for code-load failures below.
                raise
            except BaseException as exc:
                # Fail-soft contract (user ruling 2026-09-11): report this
                # provider loudly and keep the others. `_load_one` already
                # dropped the half-executed module from sys.modules.
                plugin_load_report.report_plugin_load_failure(name, exc)
        if not provider_api.REGISTRY.bindings:
            raise RuntimeError(
                "no provider plugins enabled — enable at least one provider plugin "
                "(the repo ships the lm_* default set; check the plugin enable config)"
            )
        anthropic_protocol_by_model = {
            model_id: binding.anthropic_protocol
            for prefix, binding in provider_api.REGISTRY.bindings.items()
            for model_id in model_registry.MODELS
            if model_id.startswith(prefix)
        }
        model_registry._validate_registry(anthropic_protocol_by_model=anthropic_protocol_by_model)
        _STATE.loaded = True


def _reset_loaded_for_tests() -> None:
    """Clear the once-per-process flag — test support only.

    Tests that exercise the loader against fixture plugin dirs reset between
    cases; no production path calls this.
    """
    with _lock:
        _STATE.loaded = False

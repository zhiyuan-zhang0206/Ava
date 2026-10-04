"""Lazy loader for provider plugins — the one place a plugin's ``provider.py``
is imported.

Plugin provider registration must happen in *every* process that builds or
validates a chat model — the agent, the gateway (spawn validation + model
lists), the labeler daemon, the eval harness — none of which loads
``plugin.py`` (that loader is agent-process-only, and layering forbids
``base`` from importing it). This loader lives in ``base``, reuses the
existing plugin discovery + enable config, and imports only ``provider.py``
(shared-only dependencies), the same standalone-by-path idiom as
``default_config.py`` (``base/packages/plugins/enable_config.py:update_all_disk_images``).

Loaded once per process, on the first catalog-consulting call (`model_catalog()`:
``build_chat_model`` / ``validate_model_config`` / ``get_models`` / ``resolve_context_budget`` /
``resolve_available_model`` / the config-overlay validation / the gateway's per-model views). Import
order is sorted plugin names — deterministic rather than filesystem-order. A provider.py whose module
body raises is contained with a loud report (``base.packages.plugins.load_report``): the failure is
recorded, the rest of that module is abandoned, and the remaining providers still load — the
fail-soft contract (user ruling 2026-09-11): one broken plugin's provider code must not take down
every process that builds a model. The half-executed module is dropped from ``sys.modules``, so a
later attempt re-executes the module body from the top. One exception, deliberately fail-closed: a
registration-contract violation (`provider_api.ProviderRegistrationError` — duplicate/nested prefix,
mismatched model, bad price data) propagates, because the flat prefix and model-id maps cannot pick a
winner between two claimants. The catalog is built in a throwaway `CatalogBuilder`, so a failed
attempt leaves nothing behind and a retry starts clean.

Core registers no providers. At least one enabled provider plugin must bind at load time; an empty
catalog still raises before the process's catalog is set (a configuration failure is not
contained), so correcting the enable configuration can be retried in the same process.

The process's catalog lives in one slot here, written only by `_CatalogSlot.swap`: `model_catalog()`
fills it, `use_catalog` lends a different one for the duration of a block (tests and tooling).
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from base.lm.catalog import CatalogBuilder, ModelCatalog
from base.packages.plugins import load_report


class _CatalogSlot:
    """Holder of the process's model catalog, kept off the module global namespace."""

    def __init__(self) -> None:
        self.catalog: ModelCatalog | None = None

    def swap(self, catalog: ModelCatalog | None) -> ModelCatalog | None:
        """Set the slot and return what it held: its only writer."""
        previous, self.catalog = self.catalog, catalog
        return previous


_lock = threading.Lock()
_STATE = _CatalogSlot()


def model_catalog() -> ModelCatalog:
    """The process's model catalog, built from the enabled provider plugins on first call.

    Idempotent and thread-safe (the gateway serves spawn endpoints from a thread pool; two
    concurrent first calls must not build twice). Core contributes no fallback binding, so
    loading zero providers is a retryable startup error.
    """
    catalog = _STATE.catalog
    if catalog is not None:
        return catalog
    with _lock:
        catalog = _STATE.catalog
        if catalog is None:
            catalog = _build_catalog()
            _STATE.swap(catalog)
        return catalog


@contextmanager
def use_catalog(catalog: ModelCatalog | None) -> Generator[None]:
    """Run a block against `catalog` instead of the process's own, then restore it.

    `None` empties the slot, so the block's first `model_catalog()` loads the provider plugins
    afresh (the provider-plugin tests). Test and tooling seam: no production path calls this.
    """
    with _lock:
        previous = _STATE.swap(catalog)
    try:
        yield
    finally:
        with _lock:
            _STATE.swap(previous)


def _load_one(builder: CatalogBuilder, name: str, provider_py: Path, *, is_builtin: bool) -> None:
    """Import one plugin's `provider.py`, take its declaration and install it into `builder`.

    The module registers nothing: it exports `contribute()` returning a `PluginContributions` whose
    `providers` are `ProviderContribution`s. The declaration passes the manifest gate (the
    `providers` key of the plugin's `ava-plugin.json`, when it ships one) before anything is
    installed.
    """
    from base.lm import provider_api
    from base.packages.plugins.data_registry import declaration_of
    from base.packages.plugins.gate import check_manifest

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
    try:
        spec.loader.exec_module(module)
        contributions = declaration_of(module)
        check_manifest(name, provider_py.parent, contributions, ("providers",))
    except Exception as e:
        # A code-load failure: drop the half-executed module and wrap for the
        # loader, which contains it loudly (the same cleanup the plugin.py
        # loader's fail-soft contract performs).
        sys.modules.pop(spec.name, None)
        raise RuntimeError(f"provider plugin {name!r} failed to load ({provider_py})") from e
    try:
        for contribution in contributions.providers:
            builder.install(name, contribution)
    except provider_api.ProviderRegistrationError:
        # Fail-closed by design — the loader lets this class propagate instead
        # of containing it (a flat prefix/model map cannot pick a winner).
        # Still drop the module: a later attempt retries clean.
        sys.modules.pop(spec.name, None)
        raise


def _build_catalog() -> ModelCatalog:
    """Import every enabled plugin's ``provider.py`` into a fresh builder and build it."""
    from base import paths
    from base.lm import provider_api
    from base.packages.plugins import enable_config

    builder = CatalogBuilder()
    discovered = enable_config.discover_plugins()
    known = set(discovered)
    config = enable_config.load_for_runtime(known)
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
            _load_one(builder, name, provider_py, is_builtin=is_builtin)
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
            load_report.report_plugin_load_failure(name, exc)
    if not builder.has_bindings:
        raise RuntimeError(
            "no provider plugins enabled — enable at least one provider plugin "
            "(the repo ships the lm_* default set; check the plugin enable config)"
        )
    return builder.build()

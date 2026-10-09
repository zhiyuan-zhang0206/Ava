"""Build an immutable catalog from enabled provider declarations.

Composition roots own the returned value and supply it to their consumers.
A fresh builder contains each attempt; load and contract errors propagate with
no partial catalog installed. Disabled plugins and plugins with no provider
face contribute nothing.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from base.lm.catalog import CatalogBuilder, ModelCatalog


def _load_one(builder: CatalogBuilder, name: str, provider_py: Path, *, is_builtin: bool) -> None:
    """Import one plugin's `provider.py`, take its declaration and install it into `builder`.

    The module registers nothing: it exports `contribute()` returning a `PluginContributions` whose
    `providers` are `ProviderContribution`s. The declaration passes the manifest gate (the
    `providers` key of the plugin's `ava-plugin.json`, when it ships one) before anything is
    installed.
    """
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
        for contribution in contributions.providers:
            builder.install(name, contribution)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise


def build_model_catalog() -> ModelCatalog:
    """Import every enabled plugin's ``provider.py`` into a fresh builder and build it."""
    from base import paths
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
        _load_one(builder, name, provider_py, is_builtin=is_builtin)
    if not builder.has_bindings:
        raise RuntimeError(
            "no provider plugins enabled — enable at least one provider plugin "
            "(the repo ships the lm_* default set; check the plugin enable config)"
        )
    return builder.build()

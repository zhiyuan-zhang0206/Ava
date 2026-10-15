from __future__ import annotations

import sys
from dataclasses import replace

from base.host.env.agent_slices import ModelOverrides
from base.lm.context_budget import resolve_context_budget
from base.lm.registry import resolve_available_model
from base.packages import plugin_providers


def emit(text: object) -> None:
    sys.stdout.write(f"{text}\n")


def withdrawal() -> None:
    assert not hasattr(plugin_providers, "_STATE")
    catalog = plugin_providers.build_model_catalog()
    assert resolve_available_model("deepseek-flash", models=catalog.models) == "deepseek-flash"
    withdrawn = replace(
        catalog.models["deepseek-flash"],
        spawnable=False,
        unavailable_fallback="deepseek-flash",
    )
    changed = replace(catalog, models={**catalog.models, "deepseek-retired-fixture": withdrawn})
    assert (
        resolve_available_model("deepseek-retired-fixture", models=changed.models)
        == "deepseek-flash"
    )
    assert "deepseek-retired-fixture" not in catalog.models
    emit("OK")


def budget(model: str) -> None:
    assert not hasattr(plugin_providers, "_STATE")
    catalog = plugin_providers.build_model_catalog()
    result = resolve_context_budget(model, catalog=catalog, overrides=ModelOverrides.from_pins({}))
    emit(result.max_context_tokens)


if __name__ == "__main__":
    if sys.argv[1] == "withdrawal":
        withdrawal()
    else:
        budget(sys.argv[2])

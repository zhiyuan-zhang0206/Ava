"""Probes of what the exec child's start path leaves in `sys.modules`; run by test_lazy_child_imports.

`python -I -B -X utf8 child_lazy_imports.py <probe> [<argument> ...]` imports what one scenario
imports, in a fresh interpreter, and prints one JSON report on its last line. Every probe imports
inside its own function: a module-level import would change what the probe measures.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

_LANGCHAIN = ("langchain", "langgraph", "langsmith")


def _langchain_modules() -> list[str]:
    return sorted(name for name in sys.modules if name.startswith(_LANGCHAIN))


def graph_light() -> dict[str, Any]:
    import agent.graph.exec.protocol  # noqa: F401  # pyright: ignore[reportUnusedImport]

    heavy = sorted(
        name
        for name in sys.modules
        if name
        in (
            "agent.graph._build",
            "agent.graph.claim.node",
            "agent.graph.llm.node",
            "agent.graph.exec.node",
        )
    )
    return {"heavy": heavy, "langchain": len(_langchain_modules())}


def stateless_request() -> dict[str, Any]:
    import tempfile

    from agent.graph.exec.protocol import read_request

    req = Path(tempfile.mkdtemp(prefix="lazy-child-")) / "req-x.json"
    req.write_text(
        json.dumps(
            {
                "v": 1,
                "code": "pass",
                "agent_id": None,
                "context": {
                    "identity": {"agent_id": None, "owns_loop": True, "actor": None},
                    "gateway_url": "http://gateway.invalid:8000",
                },
                "timeout_s": 1.0,
            }
        ),
        encoding="utf-8",
    )
    payload = read_request(req)
    serde = sorted(
        name
        for name in sys.modules
        if name in ("langgraph.checkpoint.serde.jsonplus", "agent.state")
    )
    return {"state": payload.state, "loaded": serde}


def process_boot_import() -> dict[str, Any]:
    import agent.process_boot  # noqa: F401  # pyright: ignore[reportUnusedImport]

    loaded = sorted(
        name
        for name in sys.modules
        if name in ("base.lm.factory", "langchain_core.language_models.chat_models")
    )
    return {"loaded": loaded, "langchain": len(_langchain_modules())}


def registry_leaf() -> dict[str, Any]:
    import base.lm.registry  # noqa: F401  # pyright: ignore[reportUnusedImport]

    loaded = sorted(
        name
        for name in sys.modules
        if name in ("base.lm.factory", "base.lm.provider_api") or name.startswith(_LANGCHAIN)
    )
    return {"loaded": loaded}


def provider_registration_surface() -> dict[str, Any]:
    import base.lm.provider_api  # noqa: F401  # pyright: ignore[reportUnusedImport]

    return {"heavy": _langchain_modules()}


def provider_plugin_load() -> dict[str, Any]:
    from base.packages.plugin_providers import build_model_catalog

    catalog = build_model_catalog()
    return {"bindings": len(catalog.bindings), "heavy": _langchain_modules()}


def graph_reexports() -> dict[str, Any]:
    import agent.graph as graph_package

    exported: Any = graph_package  # `from agent.graph import x` reads the same lazy `__getattr__`
    functions = [exported.build_graph, exported.claim_node, exported.exec_node, exported.llm_node]
    note = exported.EXEC_CANCEL_NOTE
    return {
        "identity": exported.build_graph.__module__ == "agent.graph._build",
        "callable": all(callable(fn) for fn in functions),
        "note": isinstance(note, str) and bool(note),
    }


_PLUGIN_ROOTS = ("meiva_builtins.plugins.", "plugins.")


def _plugin_modules(suffix: str) -> list[str]:
    return sorted(
        name for name in sys.modules if name.endswith(suffix) and name.startswith(_PLUGIN_ROOTS)
    )


def plugin_surface_load() -> dict[str, Any]:
    import meiva

    meiva.ensure_plugins_loaded(surface=True)
    agent_side = sorted(
        name
        for name in sys.modules
        if name == "agent.state" or name.startswith(("agent.graph", "agent.hooks"))
    )
    return {
        "agent_side": agent_side,
        "heavy": _langchain_modules(),
        "faces": _plugin_modules(".agent_runtime"),
        "surfaces": _plugin_modules(".plugin"),
    }


def plugin_surface_upgrade() -> dict[str, Any]:
    import meiva

    meiva.ensure_plugins_loaded(surface=True)
    surface = sys.modules["meiva_builtins.plugins.meiva_memory.plugin"]
    meiva.ensure_plugins_loaded(surface=False)  # the stateful-request upgrade
    return {
        "same_surface_identity": sys.modules["meiva_builtins.plugins.meiva_memory.plugin"]
        is surface,
        "faces": _plugin_modules(".agent_runtime"),
    }


def stateful_request_raw(req: str) -> dict[str, Any]:
    from agent.graph.exec.protocol import read_request

    payload = read_request(Path(req))
    return {
        "state": payload.state,
        "raw": payload.state_raw is not None,
        "agent_state": "agent.state" in sys.modules,
        "heavy": _langchain_modules(),
    }


def _snapshot(tag: str) -> dict[str, Any]:
    from meiva.sdk_surface import process_context

    # Child imports may continue while the report is filtered. Observe one table.
    modules = sys.modules.copy()
    return {
        "tag": tag,
        "state_is_none": not process_context.in_exec_turn(),
        "agent_state": "agent.state" in modules,
        "faces": any(
            name.endswith(".agent_runtime") and name.startswith(_PLUGIN_ROOTS) for name in modules
        ),
        "heavy": sorted(name for name in modules if name.startswith(_LANGCHAIN)),
    }


class _ImportingModuleName(str):
    """A `sys.modules` key that imports something when the snapshot filters it."""

    def startswith(self, prefix: Any, *args: Any) -> bool:  # pyright: ignore[reportIncompatibleMethodOverride]
        sys.modules["lazy_child_snapshot_added"] = None  # pyright: ignore[reportArgumentType]
        return super().startswith(prefix, *args)


def child_state_lazy(req: str, mutate_module_names: str) -> dict[str, Any]:
    import meiva
    from agent.execution import child as exec_child
    from agent.graph.exec.protocol import read_request

    child = exec_child.import_runtime(0.0)  # the child boot's step that binds the SDK
    payload = read_request(Path(req))
    meiva.ensure_plugins_loaded()
    boot = _snapshot("boot")
    exec_child.build_state_slot(child, payload)
    if mutate_module_names == "mutate":
        # Force an import-table change during filtering, without timing or retries.
        sys.modules[_ImportingModuleName("lazy_child_snapshot_trigger")] = None  # pyright: ignore[reportArgumentType]
        armed = _snapshot("armed")
        assert "lazy_child_snapshot_added" in sys.modules
        del sys.modules["lazy_child_snapshot_trigger"]
        del sys.modules["lazy_child_snapshot_added"]
    else:
        armed = _snapshot("armed")
    meiva.state.materialize()
    return {"boot": boot, "armed": armed, "touched": _snapshot("touched")}


def child_cwd_set_first_touch(req: str, arg: str) -> dict[str, Any]:
    import meiva
    from agent.execution import child as exec_child
    from agent.graph.exec.protocol import read_request

    child = exec_child.import_runtime(0.0)  # the child boot's step that binds the SDK
    payload = read_request(Path(req))
    meiva.ensure_plugins_loaded()
    exec_child.build_state_slot(child, payload)

    out: dict[str, Any] = {}
    try:
        meiva.cwd.set(arg)  # the very first state touch — no warm-up read
        out["set"] = "ok"
    except Exception as e:  # the report carries the failure (type + message)
        out["set"] = f"{type(e).__name__}: {e}"
    out["state_update_keys"] = None if meiva.state_update is None else sorted(meiva.state_update)
    out["cwd_after"] = str(meiva.cwd.get())
    return out


PROBES: dict[str, Callable[..., dict[str, Any]]] = {
    "graph_light": graph_light,
    "stateless_request": stateless_request,
    "process_boot_import": process_boot_import,
    "registry_leaf": registry_leaf,
    "provider_registration_surface": provider_registration_surface,
    "provider_plugin_load": provider_plugin_load,
    "graph_reexports": graph_reexports,
    "plugin_surface_load": plugin_surface_load,
    "plugin_surface_upgrade": plugin_surface_upgrade,
    "stateful_request_raw": stateful_request_raw,
    "child_state_lazy": child_state_lazy,
    "child_cwd_set_first_touch": child_cwd_set_first_touch,
}

if __name__ == "__main__":
    sys.stdout.write(json.dumps(PROBES[sys.argv[1]](*sys.argv[2:])) + "\n")

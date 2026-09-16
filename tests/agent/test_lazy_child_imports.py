"""Child start path stays off the agent-side heavy chains — PR-2 of the
startup-path laziness work (task #3585).

The exec child imports `agent.graph._exec_protocol` (request envelope),
`agent._process_boot` (SDK helpers), and the media-gate resolution on
`shared.lm.registry` before any user code runs. Each probe runs in a clean
subprocess (isolated interpreter, agent-launch env vars stripped, repo root
prepended to `sys.path`) and reports the heavy modules the touch left in
`sys.modules`:

- the `agent.graph` package init is lazy (PEP 562): importing a light
  submodule must not pull the node set (`_build`/`_claim`/`_llm`/`_exec`) or
  any langchain/langgraph module;
- `read_request` on a state-less envelope must not pull the langgraph serde
  or `agent.state` (they load only when a state snapshot exists);
- importing `agent._process_boot` must stay off the LM stack;
- importing `shared.lm.registry` (the media-capability data leaf) must not
  pull `shared.lm.factory` / `shared.lm.provider_api`;
- the provider-registration surface (`shared.lm.provider_api` plus the `lm_*`
  provider plugins loaded by `ensure_provider_plugins_loaded`) must stay off
  the LM chat-model stack (task #3633);
- the child's plugin autoload surface form (`ava._ensure_plugins_loaded(surface=True)`)
  must load the plugin surfaces only — no agent-runtime faces, no `agent.state`
  / `agent.hooks` / `agent.graph.*`, no graph/LM stack — and a later full call
  must upgrade by loading faces only, never re-executing a surface (task #3633,
  leg-1 B6);
- a stateful request reads raw and its child boot arms a lazy state slot —
  `read_request` + the surface load stay off the serde / `agent.state` / the
  faces; the first state use (materialize) resolves them (task #3633, leg-2 B3).

The lazy re-export stays a working API: `from agent.graph import build_graph`
and friends resolve through `__getattr__` (the functional probe).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Agent-launch markers: with AVA_AGENT_ID forwarded, `import ava` self-loads
# plugin namespaces (`_boot.is_launched_child`), inflating the import by ~13MB
# and adding plugin modules (recon #3586 §2.6). A boot measurement must strip
# them — mirroring measure-boot.sh's clean env.
_CLEAN_ENV_STRIP = frozenset(
    {
        "AVA_AGENT_ID",
        "AVA_RUNNER_MODE",
        "AVA_PROCESS_PROFILE",
        "AVA_EXEC_REQUEST_FILE",
        "AVA_EXEC_RESULT_FILE",
        "AVA_EXEC_TIMEOUT_S",
        "AVA_TURN_ID",
        "AVA_SESSION_ID",
        "AVA_LOG_DIR",
        "AVA_AGENT_LABEL",
        "AVA_AGENT_DIR",
    }
)

_LANGCHAIN_PREFIXES = ("langchain", "langgraph", "langsmith")


def _run_clean_probe(body: str) -> dict[str, object]:
    """Run `body` in a clean subprocess; return its JSON probe report."""
    code = f"import json\nimport sys\n\nsys.path.insert(0, {str(_REPO_ROOT)!r})\n{body}"
    env = {key: value for key, value in os.environ.items() if key not in _CLEAN_ENV_STRIP}
    proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
        [sys.executable, "-I", "-B", "-X", "utf8", "-c", code],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


_GRAPH_LIGHT = """
import importlib

importlib.import_module("agent.graph._exec_protocol")
heavy = sorted(
    name
    for name in sys.modules
    if name
    in (
        "agent.graph._build",
        "agent.graph._claim",
        "agent.graph._llm",
        "agent.graph._exec",
    )
)
langchain = sum(1 for name in sys.modules if name.startswith(("langchain", "langgraph", "langsmith")))
print(json.dumps({"heavy": heavy, "langchain": langchain}))
"""


def test_agent_graph_init_stays_lazy_for_light_submodules() -> None:
    report = _run_clean_probe(_GRAPH_LIGHT)
    assert report["heavy"] == [], f"node set pulled by the package init: {report['heavy']}"
    assert report["langchain"] == 0, "the package init must not enter langchain"


_STATELESS_REQUEST = """
import tempfile
from pathlib import Path

from agent.graph._exec_protocol import read_request

req = Path(tempfile.mkdtemp(prefix="lazy-child-")) / "req-x.json"
req.write_text(
    json.dumps({"v": 1, "code": "pass", "agent_id": None, "timeout_s": 1.0}), encoding="utf-8"
)
payload = read_request(req)
serde = sorted(
    name for name in sys.modules if name in ("langgraph.checkpoint.serde.jsonplus", "agent.state")
)
print(json.dumps({"state": payload.state, "loaded": serde}))
"""


def test_stateless_request_skips_the_serde() -> None:
    report = _run_clean_probe(_STATELESS_REQUEST)
    assert report["state"] is None
    assert report["loaded"] == [], (
        f"serde/agent.state loaded without a snapshot: {report['loaded']}"
    )


_PROCESS_BOOT_IMPORT = """
import agent._process_boot  # noqa: F401

loaded = sorted(
    name
    for name in sys.modules
    if name in ("shared.lm.factory", "langchain_core.language_models.chat_models")
)
langchain = sum(1 for name in sys.modules if name.startswith(("langchain", "langgraph", "langsmith")))
print(json.dumps({"loaded": loaded, "langchain": langchain}))
"""


def test_import_process_boot_stays_off_the_lm_stack() -> None:
    report = _run_clean_probe(_PROCESS_BOOT_IMPORT)
    assert report["loaded"] == [], f"LM stack pulled by the import: {report['loaded']}"
    assert report["langchain"] == 0, "importing the boot helpers must not enter langchain"


_REGISTRY_LEAF = """
import shared.lm.registry  # noqa: F401

loaded = sorted(
    name
    for name in sys.modules
    if name in ("shared.lm.factory", "shared.lm.provider_api")
    or name.startswith(("langchain", "langgraph", "langsmith"))
)
print(json.dumps({"loaded": loaded}))
"""


def test_registry_media_resolution_is_a_data_leaf() -> None:
    report = _run_clean_probe(_REGISTRY_LEAF)
    assert report["loaded"] == [], f"data leaf pulled the provider stack: {report['loaded']}"


_PROVIDER_REGISTRATION_SURFACE = """
import shared.lm.provider_api  # noqa: F401

heavy = sorted(
    name for name in sys.modules if name.startswith(("langchain", "langgraph", "langsmith"))
)
print(json.dumps({"heavy": heavy}))
"""


def test_provider_registration_surface_stays_off_the_lm_stack() -> None:
    report = _run_clean_probe(_PROVIDER_REGISTRATION_SURFACE)
    assert report["heavy"] == [], f"the registration surface pulled the LM stack: {report['heavy']}"


_PROVIDER_PLUGIN_LOAD = """
from shared.lm import provider_api
from shared.lm._plugin_providers import ensure_provider_plugins_loaded

ensure_provider_plugins_loaded()
heavy = sorted(
    name for name in sys.modules if name.startswith(("langchain", "langgraph", "langsmith"))
)
print(json.dumps({"bindings": len(provider_api.REGISTRY.bindings), "heavy": heavy}))
"""


def test_loading_provider_plugins_stays_off_the_lm_stack() -> None:
    report = _run_clean_probe(_PROVIDER_PLUGIN_LOAD)
    assert report["bindings"] != 0, "provider registration did not run — the probe is vacuous"
    assert report["heavy"] == [], f"provider plugins pulled the LM stack: {report['heavy']}"


_GRAPH_REEXPORTS = """
from agent.graph import EXEC_CANCEL_NOTE, build_graph, claim_node, exec_node, llm_node

import agent.graph._build as build_module

print(
    json.dumps(
        {
            "identity": build_graph is build_module.build_graph,
            "callable": all(callable(fn) for fn in (build_graph, claim_node, exec_node, llm_node)),
            "note": isinstance(EXEC_CANCEL_NOTE, str) and bool(EXEC_CANCEL_NOTE),
        }
    )
)
"""


def test_graph_reexports_resolve_through_the_lazy_getattr() -> None:
    report = _run_clean_probe(_GRAPH_REEXPORTS)
    assert report == {"identity": True, "callable": True, "note": True}


def test_factory_reexports_the_registry_resolution() -> None:
    from shared.lm import factory, registry

    assert factory.media_types_for_model is registry.media_types_for_model
    assert factory.attach_modalities_for_model is registry.attach_modalities_for_model


# ── leg-1 (B6): the child's plugin autoload ────────────────────────────────

_PLUGIN_SURFACE_LOAD = """
import ava

ava._ensure_plugins_loaded(surface=True)

_PLUGIN_ROOTS = ("ava_builtins.plugins.", "plugins.")


def _plugin_modules(suffix):
    return sorted(
        name
        for name in sys.modules
        if name.endswith(suffix) and name.startswith(_PLUGIN_ROOTS)
    )


agent_side = sorted(
    name
    for name in sys.modules
    if name == "agent.state" or name.startswith(("agent.graph", "agent.hooks"))
)
heavy = sorted(
    name for name in sys.modules if name.startswith(("langchain", "langgraph", "langsmith"))
)
print(
    json.dumps(
        {
            "agent_side": agent_side,
            "heavy": heavy,
            "faces": _plugin_modules(".agent_runtime"),
            "surfaces": _plugin_modules(".plugin"),
        }
    )
)
"""


def test_child_surface_load_stays_off_the_agent_runtime() -> None:
    report = _run_clean_probe(_PLUGIN_SURFACE_LOAD)
    surfaces = cast("list[object]", report["surfaces"])
    assert "ava_builtins.plugins.ava_memory.plugin" in surfaces, (
        f"the surface load did not run — the probe is vacuous: {surfaces}"
    )
    assert report["faces"] == [], f"the surface load pulled agent-runtime faces: {report['faces']}"
    assert report["agent_side"] == [], (
        f"the surface load pulled the agent runtime: {report['agent_side']}"
    )
    assert report["heavy"] == [], f"the surface load pulled the graph/LM stack: {report['heavy']}"


_PLUGIN_SURFACE_UPGRADE = """
import sys

import ava

ava._ensure_plugins_loaded(surface=True)
surface = sys.modules["ava_builtins.plugins.ava_memory.plugin"]

ava._ensure_plugins_loaded(surface=False)  # the stateful-request upgrade
faces = sorted(
    name
    for name in sys.modules
    if name.endswith(".agent_runtime")
    and name.startswith(("ava_builtins.plugins.", "plugins."))
)
print(
    json.dumps(
        {
            "same_surface_identity": sys.modules["ava_builtins.plugins.ava_memory.plugin"]
            is surface,
            "faces": faces,
        }
    )
)
"""


def test_child_surface_upgrade_loads_faces_without_reexecuting_surfaces() -> None:
    report = _run_clean_probe(_PLUGIN_SURFACE_UPGRADE)
    faces = cast("list[object]", report["faces"])
    assert report["same_surface_identity"] is True, "the upgrade re-executed the surface module"
    assert "ava_builtins.plugins.ava_memory.agent_runtime" in faces, (
        f"the upgrade loaded no faces — the probe is vacuous: {faces}"
    )


# ── leg-2 (B3): the stateful child's lazy state slot ───────────────────────


def _craft_stateful_envelope(tmp_path: Path) -> Path:
    """A v1 request envelope carrying a typed state snapshot."""
    import base64

    from agent.graph._exec_protocol import dumps_typed

    tag, blob = dumps_typed({"ava_code__cwd": str(tmp_path)})
    req = tmp_path / "req-state.json"
    req.write_text(
        json.dumps(
            {
                "v": 1,
                "code": "pass",
                "agent_id": None,
                "timeout_s": 1.0,
                "state_tag": tag,
                "state_b64": base64.b64encode(blob).decode("ascii"),
            }
        ),
        encoding="utf-8",
    )
    return req


_STATEFUL_REQUEST_RAW = """
from pathlib import Path

from agent.graph._exec_protocol import read_request

payload = read_request(Path({req!r}))
heavy = sorted(
    name for name in sys.modules if name.startswith(("langchain", "langgraph", "langsmith"))
)
print(
    json.dumps(
        {{
            "state": payload.state,
            "raw": payload.state_raw is not None,
            "agent_state": "agent.state" in sys.modules,
            "heavy": heavy,
        }}
    )
)
"""


def test_stateful_request_reads_raw_and_stays_off_the_serde(tmp_path: Path) -> None:
    report = _run_clean_probe(
        _STATEFUL_REQUEST_RAW.format(req=str(_craft_stateful_envelope(tmp_path)))
    )
    assert report["raw"] is True, "the stateful envelope did not carry a raw blob"
    assert report["state"] is None, "read_request decoded eagerly — leg-2 wants it raw"
    assert report["agent_state"] is False, "read_request pulled agent.state"
    assert report["heavy"] == [], f"read_request pulled the serde/LM stack: {report['heavy']}"


_CHILD_STATE_LAZY = """
from pathlib import Path

import ava
from agent import exec_child
from agent.graph._exec_protocol import read_request

exec_child._import_runtime()  # the child boot's step that binds the SDK (mirrors `_run`)


def _snap(tag):
    return {{
        "tag": tag,
        "state_is_none": ava.state is None,
        "agent_state": "agent.state" in sys.modules,
        "faces": any(
            name.endswith(".agent_runtime")
            and name.startswith(("ava_builtins.plugins.", "plugins."))
            for name in sys.modules
        ),
        "heavy": sorted(
            name
            for name in sys.modules
            if name.startswith(("langchain", "langgraph", "langsmith"))
        ),
    }}


payload = read_request(Path({req!r}))
ava._ensure_plugins_loaded()
boot = _snap("boot")
exec_child._build_state_slot(payload)
armed = _snap("armed")
ava.state.materialize()
touched = _snap("touched")
print(json.dumps({{"boot": boot, "armed": armed, "touched": touched}}))
"""


def test_stateful_child_arms_lazily_and_materializes_on_first_use(tmp_path: Path) -> None:
    report = _run_clean_probe(_CHILD_STATE_LAZY.format(req=str(_craft_stateful_envelope(tmp_path))))
    boot = cast("dict[str, object]", report["boot"])
    armed = cast("dict[str, object]", report["armed"])
    touched = cast("dict[str, object]", report["touched"])
    assert boot["state_is_none"] is True  # the surface boot has not armed a slot yet
    assert armed["state_is_none"] is False  # the stateful request armed the lazy slot
    for snap, tag in ((boot, "boot"), (armed, "armed")):
        assert snap["agent_state"] is False, f"{tag} pulled agent.state"
        assert snap["faces"] is False, f"{tag} pulled agent-runtime faces"
        assert snap["heavy"] == [], f"{tag} pulled the serde/LM stack: {snap['heavy']}"
    assert touched["state_is_none"] is False
    assert touched["agent_state"] is True, "materialize did not resolve the state"
    assert touched["faces"] is True, "materialize did not load the faces"
    assert touched["heavy"] != [], "materialize did not load the serde — probe vacuous"

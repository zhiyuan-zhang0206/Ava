"""Child start path stays off the agent-side heavy chains — PR-2 of the
startup-path laziness work (task #3585).

The exec child imports `agent.graph.exec.protocol` (request envelope),
`agent.process_boot` (SDK helpers), and the media-gate resolution on
`base.lm.registry` before any user code runs. Each probe runs in a clean
subprocess (isolated interpreter, agent-launch env vars stripped, repo root
prepended to `sys.path`) and reports the heavy modules the touch left in
`sys.modules`:

- the `agent.graph` package init is lazy (PEP 562): importing a light
  submodule must not pull the node set (`_build`/`claim.node`/`llm.node`/`exec.node`) or
  any langchain/langgraph module;
- `read_request` on a state-less envelope must not pull the langgraph serde
  or `agent.state` (they load only when a state snapshot exists);
- importing `agent.process_boot` must stay off the LM stack;
- importing `base.lm.registry` (the media-capability data leaf) must not
  pull `base.lm.factory` / `base.lm.provider_api`;
- the provider-registration surface (`base.lm.provider_api` plus the `lm_*`
  provider plugins loaded by `model_catalog`) must stay off
  the LM chat-model stack (task #3633);
- the child's plugin autoload surface form (`meiva.ensure_plugins_loaded(surface=True)`)
  must load the plugin surfaces only — no agent-runtime faces, no `agent.state`
  / `agent.hooks` / `agent.graph.*`, no graph/LM stack — and a later full call
  must upgrade by loading faces only, never re-executing a surface (task #3633,
  leg-1 B6);
- a stateful request reads raw and its child boot arms a lazy state slot —
  `read_request` + the surface load stay off the serde / `agent.state` / the
  faces; the first state use (materialize) resolves them (task #3633, leg-2 B3).
- the first state use goes through the plugin surface's own call sites too:
  `meiva.cwd.set` as the child's very first state touch (absolute and relative)
  lands both of its updates, and a stand-in handle held across materialization
  forwards to the rebound real handle (task #3665 — the update-first
  regression).

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

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROBES = Path(__file__).with_name("child_lazy_imports.py")

# Agent-launch markers: with MEIVA_AGENT_ID forwarded, `import meiva` self-loads
# plugin namespaces (`agent_identity.is_launched_child`), inflating the import by ~13MB
# and adding plugin modules (recon #3586 §2.6). A boot measurement must strip
# them — mirroring measure-boot.sh's clean env.
_CLEAN_ENV_STRIP = frozenset(
    {
        "MEIVA_AGENT_ID",
        "MEIVA_PROCESS_PROFILE",
        "MEIVA_EXEC_REQUEST_FILE",
        "MEIVA_EXEC_RESULT_FILE",
        "MEIVA_EXEC_TIMEOUT_S",
        "MEIVA_TURN_ID",
        "MEIVA_SESSION_ID",
        "MEIVA_LOG_DIR",
        "MEIVA_AGENT_LABEL",
        "MEIVA_AGENT_DIR",
    }
)

_LANGCHAIN_PREFIXES = ("langchain", "langgraph", "langsmith")


def _run_probe(probe: str, *args: str) -> dict[str, object]:
    """Run one probe of `child_lazy_imports.py` in a clean subprocess; return its JSON report."""
    env = {key: value for key, value in os.environ.items() if key not in _CLEAN_ENV_STRIP}
    proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
        [sys.executable, "-I", "-B", "-X", "utf8", str(_PROBES), probe, *args],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_agent_graph_init_stays_lazy_for_light_submodules() -> None:
    report = _run_probe("graph_light")
    assert report["heavy"] == [], f"node set pulled by the package init: {report['heavy']}"
    assert report["langchain"] == 0, "the package init must not enter langchain"


def test_stateless_request_skips_the_serde() -> None:
    report = _run_probe("stateless_request")
    assert report["state"] is None
    assert report["loaded"] == [], (
        f"serde/agent.state loaded without a snapshot: {report['loaded']}"
    )


def test_import_process_boot_stays_off_the_lm_stack() -> None:
    report = _run_probe("process_boot_import")
    assert report["loaded"] == [], f"LM stack pulled by the import: {report['loaded']}"
    assert report["langchain"] == 0, "importing the boot helpers must not enter langchain"


def test_registry_media_resolution_is_a_data_leaf() -> None:
    report = _run_probe("registry_leaf")
    assert report["loaded"] == [], f"data leaf pulled the provider stack: {report['loaded']}"


def test_provider_registration_surface_stays_off_the_lm_stack() -> None:
    report = _run_probe("provider_registration_surface")
    assert report["heavy"] == [], f"the registration surface pulled the LM stack: {report['heavy']}"


def test_loading_provider_plugins_stays_off_the_lm_stack() -> None:
    report = _run_probe("provider_plugin_load")
    assert report["bindings"] != 0, "provider registration did not run — the probe is vacuous"
    assert report["heavy"] == [], f"provider plugins pulled the LM stack: {report['heavy']}"


def test_graph_reexports_resolve_through_the_lazy_getattr() -> None:
    report = _run_probe("graph_reexports")
    assert report == {"identity": True, "callable": True, "note": True}


def test_factory_reexports_the_registry_resolution() -> None:
    from base.lm import factory, registry

    assert factory.media_types_for_model is registry.media_types_for_model
    assert factory.attach_modalities_for_model is registry.attach_modalities_for_model


# ── leg-1 (B6): the child's plugin autoload ────────────────────────────────


def test_child_surface_load_stays_off_the_agent_runtime() -> None:
    report = _run_probe("plugin_surface_load")
    surfaces = cast("list[object]", report["surfaces"])
    assert "meiva_builtins.plugins.meiva_memory.plugin" in surfaces, (
        f"the surface load did not run — the probe is vacuous: {surfaces}"
    )
    assert report["faces"] == [], f"the surface load pulled agent-runtime faces: {report['faces']}"
    assert report["agent_side"] == [], (
        f"the surface load pulled the agent runtime: {report['agent_side']}"
    )
    assert report["heavy"] == [], f"the surface load pulled the graph/LM stack: {report['heavy']}"


def test_child_surface_upgrade_loads_faces_without_reexecuting_surfaces() -> None:
    report = _run_probe("plugin_surface_upgrade")
    faces = cast("list[object]", report["faces"])
    assert report["same_surface_identity"] is True, "the upgrade re-executed the surface module"
    assert "meiva_builtins.plugins.meiva_memory.agent_runtime" in faces, (
        f"the upgrade loaded no faces — the probe is vacuous: {faces}"
    )


# ── leg-2 (B3): the stateful child's lazy state slot ───────────────────────


_NO_AGENT_CONTEXT = {
    "identity": {"agent_id": None, "owns_loop": True, "actor": None},
    "gateway_url": "http://gateway.invalid:8000",
}


def _craft_stateful_envelope(tmp_path: Path) -> Path:
    """A v1 request envelope carrying a typed state snapshot."""
    import base64

    from agent.graph.exec.protocol import dumps_typed

    tag, blob = dumps_typed({"meiva_code__cwd": str(tmp_path)})
    req = tmp_path / "req-state.json"
    req.write_text(
        json.dumps(
            {
                "v": 1,
                "code": "pass",
                "agent_id": None,
                "context": _NO_AGENT_CONTEXT,
                "timeout_s": 1.0,
                "state_tag": tag,
                "state_b64": base64.b64encode(blob).decode("ascii"),
            }
        ),
        encoding="utf-8",
    )
    return req


def test_stateful_request_reads_raw_and_stays_off_the_serde(tmp_path: Path) -> None:
    report = _run_probe("stateful_request_raw", str(_craft_stateful_envelope(tmp_path)))
    assert report["raw"] is True, "the stateful envelope did not carry a raw blob"
    assert report["state"] is None, "read_request decoded eagerly — leg-2 wants it raw"
    assert report["agent_state"] is False, "read_request pulled agent.state"
    assert report["heavy"] == [], f"read_request pulled the serde/LM stack: {report['heavy']}"


@pytest.mark.parametrize("mutate_module_names", [False, True])
def test_stateful_child_arms_lazily_and_materializes_on_first_use(
    tmp_path: Path, mutate_module_names: bool
) -> None:
    report = _run_probe(
        "child_state_lazy",
        str(_craft_stateful_envelope(tmp_path)),
        "mutate" if mutate_module_names else "plain",
    )
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


# ── leg-2 regression (#3665): the first state touch is an update ───────────


def test_fresh_child_cwd_set_absolute_first_touch_lands_both_updates(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    report = _run_probe(
        "child_cwd_set_first_touch", str(_craft_stateful_envelope(tmp_path)), str(target)
    )
    keys = cast("list[str]", report["state_update_keys"])
    assert report["set"] == "ok", f"cwd.set as the first state touch failed: {report['set']}"
    assert "meiva_code__cwd" in keys, f"the cwd update never reached state_update: {keys}"
    assert "meiva_code__cwd_note" in keys, f"the cwd_note update never reached state_update: {keys}"
    assert report["cwd_after"] == str(target.resolve())


def test_fresh_child_cwd_set_relative_first_touch_resolves_against_snapshot(
    tmp_path: Path,
) -> None:
    (tmp_path / "sub").mkdir()
    report = _run_probe("child_cwd_set_first_touch", str(_craft_stateful_envelope(tmp_path)), "sub")
    keys = cast("list[str]", report["state_update_keys"])
    assert report["set"] == "ok", f"cwd.set as the first state touch failed: {report['set']}"
    assert "meiva_code__cwd" in keys, f"the cwd update never reached state_update: {keys}"
    assert "meiva_code__cwd_note" in keys, f"the cwd_note update never reached state_update: {keys}"
    assert report["cwd_after"] == str((tmp_path / "sub").resolve())

"""The plugin scaffold hook — how a plugin owns its own host-side state.

A plugin opts in by shipping `setup.py` beside `plugin.py` with a zero-argument
`scaffold()`. The explicit `ava memory init` command runs it for every enabled
plugin, so runtime memory state is never touched by converge or startup.

`setup.py` is loaded on its own, NOT through `plugin.py`: plugin modules import
the agent runtime (hooks, graph, the SDK namespace), none of which exists in a
CLI process. Keeping the scaffold in a separate module is what lets the explicit
CLI command call it at all — and it means a scaffold may depend on `shared` only.

It lives beside `_converge_skills.py` because both load plugin-owned host setup
without importing the agent runtime.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType


@dataclass(frozen=True)
class ScaffoldResult:
    """Which plugins were run by explicit setup. A plugin without `setup.py`
    (or without a `scaffold` in it) is simply absent — not an error."""

    ran: list[str]


def run_plugin_scaffolds() -> ScaffoldResult:
    """Call `scaffold()` on every enabled plugin that defines one.

    Loading a plugin's `setup.py` is fail-soft (user ruling 2026-09-11): a file
    that fails to import is skipped with a loud report
    (``shared.plugin_load_report``) and the remaining plugins still run — one
    broken plugin must not block the provisioning command for every other
    plugin. Running a plugin's `scaffold()` stays fail-fast: this is explicit
    provisioning, and a scaffold that raises must stop the command rather than
    let half-provisioned state pass as success.
    """
    from shared import plugin_load_report
    from shared import plugins_config as plugins_cfg

    discovered = plugins_cfg._discover_plugins()
    config = plugins_cfg.load_for_runtime(set(discovered))

    ran: list[str] = []
    for name, entry in config.plugins.items():
        if not entry.enabled:
            continue
        setup_py = discovered[name] / "setup.py"
        if not setup_py.is_file():
            continue
        try:
            module = _load_setup_module(name, setup_py)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            plugin_load_report.report_plugin_load_failure(name, exc)
            continue
        scaffold = getattr(module, "scaffold", None)
        if scaffold is None:
            continue
        print(f"  · {name}")
        scaffold()
        ran.append(name)
    return ScaffoldResult(ran=ran)


def _load_setup_module(name: str, setup_py: Path) -> ModuleType:
    """Import a plugin's `setup.py` by path. Raises on a load failure — the
    caller reports it through the fail-soft contract."""
    spec = importlib.util.spec_from_file_location(f"ava_plugin_setup_{name}", setup_py)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"plugin {name!r}: cannot load {setup_py}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

"""Graph-edge hook system (plugin registration mechanism).

Plugin system has two layers (see decisions/2026-05-13-plugin-and-hook-layers.md):
- **SDK wrap**: SDK function wrapping — see ava/_extend.py (`ava.extend.wrap`)
- **Graph-edge hook**: here — 3 hook container Nodes register arbitrary hook functions

Public API (re-exported from `_registry`):
- `register_before_llm` / `register_before_exec` / `register_after_exec` / `register_after_init` — attach
  a `Hook` instance to the corresponding container node
- `make_hook_runner(name, default_next)` called at graph build time
- `Hook` (base class) / `HookName` / `HOOKS` + global registry. Plugin authors
  subclass `Hook` and override the typed `__call__`.

Hook registration example (in plugin's `plugin.py`):

    from agent.hooks import Hook, register_before_llm

    class WatchTokenCount(Hook):
        async def __call__(self, state, runtime, config, /):
            if est_tokens(state.messages) > 100_000:
                logger.info("[plugin] tokens high")
            return None

    register_before_llm(WatchTokenCount())

Submodules:
- `agent.hooks.compact` — compact's `generate_summary()` utility function,
  used by the claim node. Actual hook registration lives in
  `ava_builtins/plugins/ava_compact/plugin.py`.
- `agent.hooks.repair` — dangling tool_use crash recovery: shared
  detection/rebuild helper + the built-in before_llm repair hook
  (`register_repair_hooks()`, called from `build_graph`).
"""

from ._registry import (
    HOOKS,
    Hook,
    HookName,
    make_hook_runner,
    register_after_exec,
    register_after_init,
    register_before_exec,
    register_before_llm,
)

# Current built-in plugin list (lives under `ava_builtins/plugins/`).
# Only used as a test fixture: tests/agent/test_builtin_metadata.py uses it
# to confirm each name has a corresponding directory + parseable plugin.py.
# Runtime loading uses `plugins_config._discover_plugins()` filesystem scan,
# does not read this constant.
BUILTIN_PLUGINS: tuple[str, ...] = (
    "ava_fleet",
    "ava_code",
    "ava_syntax_fix",
    "ava_sdk_reminder",
    "ava_silent_idle",
    "ava_memory",
)

__all__ = [
    "BUILTIN_PLUGINS",
    "HOOKS",
    "Hook",
    "HookName",
    "make_hook_runner",
    "register_after_exec",
    "register_after_init",
    "register_before_exec",
    "register_before_llm",
]

"""Graph-edge hook system (plugin declaration mechanism).

Plugin system has two layers (see decisions/2026-05-13-plugin-and-hook-layers.md):
- **SDK wrap**: SDK function wrapping — see ava/sdk_surface/wraps.py (`ava.extend.wrap`)
- **Graph-edge hook**: here — 4 hook container Nodes run the hooks the graph build hands them

Public API (re-exported from `_registry`):
- `make_hook_runner(name, default_next, hooks)` called at graph build time with
  `(plugin, hook)` pairs in run order
- `Hook` (base class) / `HookName`. Plugin authors subclass `Hook` and override the typed
  `__call__`, then declare instances in `contribute()`.
- `agent.hooks.framework.framework_hooks()` — the framework's own hooks (repair, compact,
  capability-index drift).

Hook declaration example (in a plugin's `agent_runtime.py`):

    from agent.hooks import Hook
    from base.packages.plugins.extensions import PluginContributions

    class WatchTokenCount(Hook):
        async def __call__(self, state, runtime, config, /):
            if est_tokens(state.messages) > 100_000:
                logger.info("[plugin] tokens high")
            return None

    def contribute() -> PluginContributions:
        return PluginContributions(before_llm=(WatchTokenCount(),))

Submodules:
- `agent.hooks.compact` — compact's `generate_summary()` utility function,
  used by the claim node. Actual hook registration lives in
  `ava_builtins/plugins/ava_compact/plugin.py`.
- `agent.hooks.repair` — dangling tool_use/tool_result pairing crash recovery: shared
  detection/rebuild helper + the built-in before_llm repair hook
  (its hook is one of `framework_hooks()`).
- `agent.hooks.history_dump` — the pre-compact JSONL dump of the full
  conversation, written by every compaction path (the claim node's and
  `agent.hooks.compact`'s) before the history is wiped.
"""

from ._registry import Hook, HookName, make_hook_runner

# Current built-in plugin list (lives under `ava_builtins/plugins/`).
# Only used as a test fixture: agent/hooks/tests/test_builtin_metadata.py uses it
# to confirm each name has a corresponding directory + parseable plugin.py.
# Runtime loading uses `plugins_config.discover_plugins()` filesystem scan,
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
    "Hook",
    "HookName",
    "make_hook_runner",
]

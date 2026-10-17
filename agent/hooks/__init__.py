from ._registry import Hook, HookName, make_hook_runner

# meiva:note
# The plugin system has two layers: SDK wraps modify the behavior of the meiva namespace (declared as SdkWrap, applied through meiva/sdk_surface), and graph-edge hooks intervene in the agent execution pipeline (declared through PluginContributions, run by the hook container nodes).
# meiva:link agent/hooks/docs/plugin-registration.meiva.okf.md
# meiva:note
# BUILTIN_PLUGINS names the meiva_builtins/plugins/ meiva_* built-ins only for the metadata test — each listed name must have a plugin directory with a parseable plugin.py; runtime loading discovers plugins by filesystem scan and never reads the constant.
# meiva:link agent/hooks/docs/plugin-registration.meiva.okf.md
BUILTIN_PLUGINS: tuple[str, ...] = (
    "meiva_fleet",
    "meiva_code",
    "meiva_syntax_fix",
    "meiva_sdk_reminder",
    "meiva_silent_idle",
    "meiva_memory",
    "meiva_to_user",
)

__all__ = [
    "BUILTIN_PLUGINS",
    "Hook",
    "HookName",
    "make_hook_runner",
]

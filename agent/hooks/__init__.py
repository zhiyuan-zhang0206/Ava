from ._registry import Hook, HookName, make_hook_runner

# meiva:ref agent/hooks/docs/plugin-registration.meiva.okf.md#plugin-hook-layers
# meiva:ref agent/hooks/docs/plugin-registration.meiva.okf.md#builtin-plugins-test-fixture
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

from ._registry import Hook, HookName, make_hook_runner

# ava:ref agent/hooks/docs/plugin-registration.ava.okf.md#plugin-hook-layers
# ava:ref agent/hooks/docs/plugin-registration.ava.okf.md#builtin-plugins-test-fixture
BUILTIN_PLUGINS: tuple[str, ...] = (
    "ava_fleet",
    "ava_code",
    "ava_syntax_fix",
    "ava_sdk_reminder",
    "ava_silent_idle",
    "ava_memory",
    "ava_to_user",
)

__all__ = [
    "BUILTIN_PLUGINS",
    "Hook",
    "HookName",
    "make_hook_runner",
]

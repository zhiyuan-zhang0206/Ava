"""Which plugin is currently importing — the ContextVar both plugin
registries (config classes, SDK namespaces and the rest still registered at import) read to
auto-attribute a registration to its plugin.

`load_extensions` wraps each plugin.py import in `with PluginContext(name):`
so `register_plugin_config()` and the other import-time registrations can attribute the
registration without the plugin author passing their own name. Lives in
`base/` (contextvars-only leaf) so the agent-side and base-side registries can both read it
without an agent <-> shared cycle.
"""

from __future__ import annotations

import contextvars

# ContextVar rather than module global: independent stack per thread / async
# task; graph build running multiple plugin imports concurrently on async
# paths doesn't cross-pollute (old module-global implementation was OK
# single-threaded but would race silently under async re-entry).
_CURRENT_PLUGIN: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ava_current_plugin", default=None
)


class PluginContext:
    """Mark _CURRENT_PLUGIN during plugin.py import so import-time registrations
    attribute to the plugin.

    Cross-module public (used by `agent/graph/_build.py:load_extensions`) —
    so no underscore prefix; plugin authors may also explicitly wrap to
    achieve "my register belongs to which plugin namespace".
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._token: contextvars.Token[str | None] | None = None

    def __enter__(self) -> None:
        self._token = _CURRENT_PLUGIN.set(self.name)

    def __exit__(self, *args: object) -> None:
        assert self._token is not None  # noqa: S101 — __enter__ before __exit__ invariant
        _CURRENT_PLUGIN.reset(self._token)


def current_plugin_name() -> str | None:
    """Return the currently active plugin name (`with PluginContext("foo"):`
    block returns "foo", outside the block returns None).

    Public helper for the ava top level — `ava.register_namespace` calls it
    to put "name registered by plugin X" into error messages to help plugin
    authors debug. Directly importing the private `_CURRENT_PLUGIN`
    ContextVar would be a leaky abstraction.
    """
    return _CURRENT_PLUGIN.get()

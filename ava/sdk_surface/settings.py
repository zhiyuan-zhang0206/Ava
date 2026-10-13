"""Internal config — the SDK's composition root helpers.

`database()`, `bus()` and `agent_setting()` build what an `ava.*`
function needs from this process's settings; the connections a process holds live on its bound
`AvaContext` (`ava.context.sql` / `.redis` / `.gateway`, `base.agents.context.clients`).

URL source: `base.config.settings` is the single source of truth. An empty SQL URL names no
process resource; the default database factory refuses it before a connection can reach libpq.
"""

from collections.abc import Mapping
from typing import Any

from ava.sdk_surface import process_context
from base.agents.context.clients import DatabaseHandle
from base.clock import Clock
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog

from .installation import installed


def model_catalog() -> ModelCatalog:
    """Provider facts explicitly retained by this process's SDK installation."""
    installation = installed()
    if installation is None or installation.catalog is None:
        raise RuntimeError("the SDK installation carries no ModelCatalog")
    return installation.catalog


def config_authority() -> ConfigAuthority:
    """Fresh configuration authority retained by this process's SDK installation."""
    installation = installed()
    if installation is None or installation.authority is None:
        raise RuntimeError("the SDK installation carries no ConfigAuthority")
    return installation.authority


def clock() -> Clock:
    """Build an operation's clock from its context or installed SDK root."""

    context = process_context.ambient_context()
    if context is not None:
        return context.require_clock()
    installation = installed()
    if installation is None or installation.clock_factory is None:
        raise RuntimeError("the SDK installation carries no Clock factory")
    return installation.clock_factory()


def model_overrides() -> ModelOverrides:
    """Explicit model tuning of this SDK process, including its delivered pins."""
    from dataclasses import fields

    return ModelOverrides.from_pins(
        {item.name: agent_setting(item.name) for item in fields(ModelOverrides)}
    )


# DB_URL / REDIS_URL / GATEWAY_URL are exposed via module __getattr__ (PEP
# 562) so each access reads the current `settings.X` value rather than a
# load-time snapshot. This removes the conftest "must mutate settings before
# this module is imported" invariant: tests can flip settings.data_plane.db_url at any
# point and the next ava DB op picks it up.


def __getattr__(name: str) -> Any:
    if name == "DB_URL":
        return settings.data_plane.db_url
    if name == "REDIS_URL":
        return settings.data_plane.redis_url
    if name == "GATEWAY_URL":
        from base.cluster.machine import gateway_api_base

        return gateway_api_base()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# The SDK's composition root: the handles and the agent configuration every `ava.*` function of
# this process works with. `ava.*` is a namespace of free functions the agent's code calls inside
# its exec child (or a script one launched), so nothing can pass them a handle; they ask here. The
# exec child's settings carry its agent's overlay (applied at boot), so `agent_setting` reads that
# agent's; a process attached to an agent's native state reads that agent's pins. Nothing is cached: each call builds from the
# settings as they are now. All import lazily — `import ava` must not pull the psycopg / redis /
# live-events stacks into every exec child (task #3816).


def database() -> DatabaseHandle:
    """A fresh handle from this SDK context's original database owner."""

    return process_context.current_context().clients.database()


def bus() -> "EventBus":  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
    """The live event bus, as this process's settings name it."""
    from base.events.live.bus import EventBus

    return EventBus.from_settings()


def _attached() -> tuple[Mapping[str, Any], Any] | None:
    """The pins and plugin-config view of the agent this process attached to
    (`ava.impersonation.attach`: one attachment per process), if any: the lease its context carries."""

    context = process_context.ambient_context()
    lease = None if context is None or context.identity is None else context.identity.lease
    return None if lease is None else lease.config()


def agent_setting(name: str) -> Any:
    """One per-agent setting of this process's agent: the attached agent's pin, else the
    settings (which carry the exec child's overlay)."""
    from base.host.env.agent_slices import agent_setting

    attached = _attached()
    return agent_setting(
        name,
        attached[0] if attached else None,
        default_reader=lambda _domain, field: config_authority().service_field_value(field),
    )


# ── Plugin config hierarchical view ──
#
# `ava.sdk_surface.settings.plugins.<plugin_name>` dynamically resolves the frozen Pydantic
# BaseModel instance for the current turn's agent (bound in by
# the SDK install from the plugin's declared config; an attached agent's overrides by
# `base/packages/plugins/config_view.py`).
#
# Design:
# - SDK implementation module → not in `ava.help()`, for plugin authors not the agent
# - lazy attribute access → no cache here, so restart / test monkeypatch changes
#   to the registry are immediately visible
# - lazy import SDK installation → avoids settings import triggering an agent
#   module import (test fixture / container mode can still import ava
#   without connecting agent)


class _PluginsView:
    """`ava.sdk_surface.settings.plugins` — attribute access routes to the turn's config
    for that plugin (`base/packages/plugins/config_view.py`).

    Plugins not registered raise AttributeError listing known plugin names,
    so typos / "bind hasn't run yet" are immediately visible.
    """

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        from pydantic import BaseModel

        installation = installed()
        configs: Mapping[str, BaseModel] = {} if installation is None else installation.configs
        known = tuple(sorted(configs))
        if name not in known:
            raise AttributeError(
                f"ava.sdk_surface.settings.plugins.{name} does not exist — plugin {name!r} declares no "
                f"config, or the SDK surface is not installed yet. "
                f"Known plugins: {known or '<empty>'}"
            )
        # The attached agent's overrides over the disk image; otherwise this process's own
        # instance (the exec child's boot applied its agent's overlay to it).
        attached = _attached()
        return attached[1].config_for(name) if attached else configs[name]


plugins = _PluginsView()

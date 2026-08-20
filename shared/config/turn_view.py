"""Per-turn settings view — the agent-scoped read path for `per_agent` fields.

Implements work item (b) of `future/infra/agent-runner-as-server.md`: in the
hosted runner model many agents' turns share one process, so the process-global
`settings` singleton is the wrong place for any `per_agent=True` field. The
replacement is a **contextvar-bound pin map** resolved per agent:

    config_overlay (agents_meta)  >  birth_config (frozen fields)  >  cluster default (live)

and a read proxy (`turn_settings`) that resolves a field to its pin when the
current context carries one, falling through to the live singleton otherwise.
Only the two DB-stored maps are pinned — an unpinned field always reads the
singleton at access time, so a live cluster-default edit keeps reaching agents
exactly as the `lifecycle: live` contract promises.

Mode equivalence:

- **Process mode (today)**: nothing binds the contextvar. `turn_settings.x.y`
  is byte-for-byte `settings.x.y` — the boot-time `apply_config_overlay`
  already wrote the overlay onto the singleton. Converting a read site is a
  no-op.
- **Hosted mode (Phase 1)**: the host binds `bind_agent_config(...)` before
  creating an agent's turn task. `asyncio.create_task` copies the *current*
  context, and LangGraph runs each node in its own task copying the loop-level
  context — empirically a contextvar bound BEFORE the turn task spawns
  propagates into every node task (one bound inside a node does not reach the
  next node). So one bind at task creation covers the whole turn.

Pin values are stored RAW, exactly as `apply_config_overlay` -> `set_field`
stores them onto the singleton in process mode (plain setattr, no re-coercion)
— the two modes must never disagree about what a pinned field reads as. Values
were already validated by `validate_config_overlay` before they reached
`agents_meta`.

Scope: framework `Settings` fields only. Plugin-scope config
(`shared/plugin_config_registry._PLUGIN_CONFIGS`) is a separate process-global
with the same problem, scoped the same way by its own view —
`shared/plugin_config_view.py`.

Enforcement: `scripts/lint_turn_scoped_config.py` forbids reading a
`per_agent` field through the bare singleton from turn-scoped packages —
those reads must come through `turn_settings`.
"""

from __future__ import annotations

import contextlib
from collections.abc import Generator, Mapping
from contextvars import ContextVar
from typing import Any

import shared.config_registry as _config_registry

# The current context's pin map: flat field name -> raw pinned value. None =
# no agent bound (process mode, or host code outside any turn).
_AGENT_CONFIG_PINS: ContextVar[Mapping[str, Any] | None] = ContextVar(
    "ava_agent_config_pins", default=None
)


def resolve_agent_config_pins(
    config_overlay: Mapping[str, Any] | None,
    birth_config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge an agent's two stored config maps into the flat pin map.

    birth_config first, config_overlay on top — the same last-writer-wins merge
    `agent/_process_boot.py:_apply_per_agent_framework_config` performs against
    the singleton in process mode. Only framework `Settings` fields are kept:
    plugin-scope overlay keys (dotted `plugin.field` or plugin-owned flat keys)
    are not pinnable through this view yet and are dropped here — the hosted
    host applies them via the plugin path separately.

    Unknown keys are dropped rather than raised: the maps were validated at
    write time (`validate_config_overlay`), so an unknown key here means the
    field was deleted from Settings after the overlay was stored — the
    process-mode boot tolerates exactly this by failing only in
    apply_config_overlay's resolve, and a stored-config read must not be
    stricter than the boot that would consume it.
    """
    fields = _config_registry._fields()
    pins: dict[str, Any] = {}
    for source in (birth_config, config_overlay):
        if not source:
            continue
        for key, value in source.items():
            if key in fields:
                pins[key] = value
    return pins


@contextlib.contextmanager
def bind_agent_config(pins: Mapping[str, Any]) -> Generator[None, None, None]:
    """Bind `pins` as the current context's agent config view.

    The hosted dispatcher wraps turn-task creation in this: bind -> create the
    task (which copies the context) -> reset. Nesting rebinds cleanly (tokens
    restore the previous map on exit).
    """
    token = _AGENT_CONFIG_PINS.set(dict(pins))
    try:
        yield
    finally:
        _AGENT_CONFIG_PINS.reset(token)


def current_agent_config_pins() -> Mapping[str, Any] | None:
    """The pin map bound in the current context, or None (process mode)."""
    return _AGENT_CONFIG_PINS.get()


class _TurnDomain:
    """Read proxy for one config domain: pinned per-agent fields resolve from
    the context, everything else falls through to the live singleton."""

    __slots__ = ("_domain",)

    def __init__(self, domain: str) -> None:
        self._domain = domain

    def __getattr__(self, name: str) -> Any:
        pins = _AGENT_CONFIG_PINS.get()
        if pins is not None and name in pins:
            # Pin only wins for a field this domain actually owns — a flat pin
            # map cannot leak a same-named attribute into a foreign domain.
            fields = _config_registry._fields()
            ref = fields.get(name)
            if ref is not None and ref.domain == self._domain:
                return pins[name]
        from shared.config import settings

        return getattr(getattr(settings, self._domain), name)


class TurnSettings:
    """`settings`-shaped read proxy: `turn_settings.lm.llm_model` resolves the
    per-agent pin when the context carries one, else the live singleton.

    Stateless — the per-agent half lives in the contextvar, so one module-level
    instance serves every context. Read-only by design: writes still go through
    `shared.config.set_field` (process mode) or the stored maps (hosted mode).
    """

    __slots__ = ("_domains",)

    def __init__(self) -> None:
        object.__setattr__(self, "_domains", {})

    def __getattr__(self, name: str) -> Any:
        domains: dict[str, _TurnDomain] = object.__getattribute__(self, "_domains")
        proxy = domains.get(name)
        if proxy is None:
            from shared.config import settings

            # Delegate non-domain attributes (has_domain, profile) straight to
            # the singleton; only real domain attributes get a read proxy.
            attr = getattr(settings, name)  # raises the singleton's own AttributeError
            if name not in _config_registry._DOMAIN_ATTRS:
                return attr
            proxy = domains[name] = _TurnDomain(name)
        return proxy

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(
            "turn_settings is a read-only view — write config through "
            "shared.config.set_field (process boot) or the agent's stored "
            "config maps (agents_meta), never through the view"
        )


turn_settings = TurnSettings()

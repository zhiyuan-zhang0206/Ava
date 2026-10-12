"""The installed SDK surface as a value, and the one slot on the `ava` module that holds it.

`Installation` is what `ava.sdk_surface.install` put into the process: the admitted registry, the
expansions, the wrap layers, the skill providers, resolved plugin configs, the metering ledger, the
sampling owner, SDK-disable entries and the faces flag. It is frozen: a change builds a new value
and swaps the holder. The holder is a single attribute on the `ava` module (`__plugin_installation__`);
`installed()` reads it.

This module is the read side every SDK piece below the installer shares (`wraps`, `settings`,
`sdk_disable`, `skill_sources`, the agent kernel, tests). `install.py` is the one writer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from base.agents.sdk.call_policy import SamplingPolicyOwner
from base.clock import Clock
from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from base.packages.plugins.extensions import ExtensionRegistry

from . import ava_module


@dataclass(frozen=True)
class WrapLayer:
    """One installed wrap layer, in registration order.

    - target: dotted `ava` path, e.g. `"files.read"`.
    - plugin: the plugin that declared it.
    - wrapper: the author's `wrapper(inner, *args, **kwargs)` function — the
      introspectable artifact (`wrapper.__module__` / source say who and what).
    - chained: the closure actually installed on the namespace at this layer.
    """

    target: str
    plugin: str
    wrapper: Callable[..., Any]
    chained: Callable[..., Any]


SkillProvider = Callable[[], list[Path]]


class ExternalState(Protocol):
    """The checkpoint-compatible plugin-state codec an external attachment needs.

    `ava` sits below `agent`, so the agent layer supplies it when it installs the surface
    (`agent.extensions.plugin_state_codec` satisfies this as a module)."""

    def encode_plugin_delta(self, delta: dict[str, Any], state_cls: type[Any]) -> dict[str, str]:
        """Serialize a reducer input validated against `state_cls`, without coercing it."""
        ...

    def decode_plugin_delta(self, encoded: dict[str, Any], state_cls: type[Any]) -> dict[str, Any]:
        """Decode the codec envelope, rejecting channels `state_cls` does not have."""
        ...

    def apply_plugin_delta(self, state: Any, delta: dict[str, Any]) -> None:
        """Replay one journal entry through the registered field reducers."""
        ...

    def load_snapshot(
        self, agent_id: int, *, database: Any
    ) -> tuple[Any, dict[str, Any] | None, dict[str, Any] | None]:
        """Read native state and pinned config through `database` (a `base.db.Database`); never
        create or update a checkpoint."""
        ...


@dataclass(frozen=True)
class Installation:
    """What the installed registry put into the process, and how to take it back.

    Frozen: a change builds a new value and swaps the holder (`SLOT`) — see the
    module docstring. `wrap_layers` and the provider tuple are snapshots taken when
    the installation was built; the undos still close over the builders that filled
    them (they are only ever run once, on the way out)."""

    registry: ExtensionRegistry
    expansions: tuple[str, ...]
    wrap_layers: Mapping[str, tuple[WrapLayer, ...]]
    skill_providers: tuple[SkillProvider, ...]
    metered: tuple[tuple[Any, str], ...]
    disabled: frozenset[str]
    faces: bool
    undo: tuple[Callable[[], None], ...]
    sampling: SamplingPolicyOwner = field(default_factory=SamplingPolicyOwner)
    configs: Mapping[str, BaseModel] = field(default_factory=dict[str, BaseModel])
    catalog: ModelCatalog | None = None
    authority: ConfigAuthority | None = None
    clock_factory: Callable[[], Clock] | None = None
    producer: Callable[[], Any] | None = None
    external_state: Callable[[], ExternalState] | None = None

    def require_external_state(self) -> ExternalState:
        """The plugin-state codec the agent layer supplied, imported on first use."""
        if self.external_state is None:
            raise RuntimeError("the SDK installation carries no external-attachment state codec")
        return self.external_state()

    def require_catalog(self) -> ModelCatalog:
        """Return this installation's explicit model facts or refuse a non-model installation."""
        if self.catalog is None:
            raise RuntimeError("the SDK installation carries no ModelCatalog")
        return self.catalog


# The installation is recorded on the `ava` module object itself — the one process-wide thing it
# describes — rather than in a module global of its own. A loading value also retains an
# existing surface during a faces upgrade; a failed value retains the original error.
SLOT = "__plugin_installation__"


@dataclass(frozen=True)
class Loading:
    """An in-flight load, or its failure; neither is a successful installation."""

    installation: Installation | None = None
    failure: BaseException | None = None


LOADING = Loading()


def slot() -> Any:
    """The raw holder value: an `Installation`, a `Loading` marker, or None."""
    return getattr(ava_module(), SLOT, None)


def installed() -> Installation | None:
    """The current installation, or None before `install` / after `uninstall`."""
    value = slot()
    if isinstance(value, Loading):
        return value.installation
    return value if isinstance(value, Installation) else None


def record_disabled(disabled: frozenset[str]) -> None:
    """Record newly applied SDK-disable entries: build a new value, swap the holder."""
    current = installed()
    if current is None:
        raise RuntimeError("no SDK installation to record disable entries on")
    setattr(ava_module(), SLOT, replace(current, disabled=disabled))

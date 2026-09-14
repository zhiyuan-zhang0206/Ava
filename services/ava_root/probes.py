"""Code-side probe registry — which health probe watches which unit.

The K2 unit manifest stays a closed field set (`id` / `exec` / `restart` /
`attach`): a probe is never declared there. Resolution lives in code instead,
aligned with the two paths the per-capability watchdog daemons derive today:

- **Spec path** (`register_specs`) — a service whose spec carries a
  `healthcheck_module` gates into the health roster (the same membership rule
  the watchdog uses), and its verdict callable is the spec's `identity_probe`:
  the shared, total probe the operator surfaces already trust, so a second
  definition of "alive" cannot fork from theirs.
- **Static path** (`register` / `register_ref`) — units that never had a
  spec-derived entry (the host-policy / data-plane / native-stack class) are
  registered directly; `register_ref` takes a `"module:attribute"` string that
  resolves lazily on first probe, so registration never imports probe code
  eagerly.

The health runner still guards its boundary: a probe that raises is wrapped
into a `down` verdict there (fail-closed), never into `alive`.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol, cast

from shared.daemon_health import DaemonProbe

_log = logging.getLogger(__name__)

Probe = Callable[[], DaemonProbe]
"""A total probe: returns a verdict, never raises (the healthcheck contract)."""


class ProbeSource(Protocol):
    """The slice of a service spec the registry reads.

    Duck-typed rather than imported: the registry only needs these three facts,
    and the spec type lives a layer away.
    """

    session: str
    healthcheck_module: str | None
    identity_probe: Probe | None


class ProbeError(ValueError):
    """A probe registration or reference is malformed or cannot be resolved."""


@dataclass(slots=True)
class _Entry:
    """One registration: a probe callable, or a lazy reference to one."""

    probe: Probe | None = None
    ref: str | None = None


def _split_ref(ref: str) -> tuple[str, str]:
    """`"module:attribute"` into its parts; anything else is a wiring bug."""
    module_name, sep, attribute = ref.partition(":")
    if not sep or not module_name or not attribute or ":" in attribute:
        raise ProbeError(f"probe reference {ref!r} is not 'module:attribute'")
    return module_name, attribute


def _resolve_ref(ref: str) -> Probe:
    """Import the module a reference names and return its attribute.

    Resolution failures surface as `ProbeError`; the runner treats an
    unresolvable probe as "no verdict available" and never restarts a unit it
    cannot verify (fail-closed).
    """
    module_name, attribute = _split_ref(ref)
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise ProbeError(f"cannot import probe module {module_name!r}: {exc}") from exc
    try:
        resolved = getattr(module, attribute)
    except AttributeError as exc:
        raise ProbeError(f"module {module_name!r} has no attribute {attribute!r}") from exc
    if not callable(resolved):
        raise ProbeError(f"probe reference {ref!r} resolves to a non-callable")
    return cast("Probe", resolved)


class ProbeRegistry:
    """`unit id -> probe`, resolved from code (specs or static registrations).

    Registration is fail-fast: a duplicate unit id is rejected rather than
    silently overwritten — two probes for one unit is a wiring bug, and keeping
    the last one written would hide it.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def register(self, unit_id: str, probe: Probe) -> None:
        """Register `probe` for `unit_id` (the static path, direct callable)."""
        self._reject_duplicate(unit_id)
        self._entries[unit_id] = _Entry(probe=probe)

    def register_ref(self, unit_id: str, ref: str) -> None:
        """Register `unit_id` against `"module:attribute"`, resolved lazily.

        The reference is validated eagerly (fail-fast) but the module is only
        imported when the unit is first probed, so registering the
        host-policy / data-plane class costs no imports until the wiring slice
        actually runs those probes.
        """
        self._reject_duplicate(unit_id)
        _split_ref(ref)
        self._entries[unit_id] = _Entry(ref=ref)

    def register_specs(self, specs: Iterable[ProbeSource]) -> None:
        """Register every spec that declares a `healthcheck_module`.

        A spec without one is not health-monitored (the watchdog daemons
        themselves); a spec whose `identity_probe` is unset has no shared
        verdict surface yet and is skipped with a debug line — its probe
        adapter is a wiring-time decision, and inventing one here would fork
        the verdict contract.
        """
        for spec in specs:
            if spec.healthcheck_module is None:
                continue
            if spec.identity_probe is None:
                _log.debug(
                    "[probes] unit %s declares %s but no identity probe; no health entry",
                    spec.session,
                    spec.healthcheck_module,
                )
                continue
            self.register(spec.session, spec.identity_probe)

    def unit_ids(self) -> tuple[str, ...]:
        """Registered unit ids, in registration order (the round's order)."""
        return tuple(self._entries)

    def resolve(self, unit_id: str) -> Probe:
        """The probe for `unit_id`; resolves and caches a lazy reference."""
        try:
            entry = self._entries[unit_id]
        except KeyError:
            raise ProbeError(f"no probe registered for unit {unit_id!r}") from None
        if entry.probe is None:
            ref = entry.ref
            if ref is None:
                raise ProbeError(f"probe entry for unit {unit_id!r} carries neither probe nor ref")
            entry.probe = _resolve_ref(ref)
        return entry.probe

    def _reject_duplicate(self, unit_id: str) -> None:
        if not unit_id:
            raise ProbeError("unit id must be a non-empty string")
        if unit_id in self._entries:
            raise ProbeError(f"a probe for unit {unit_id!r} is already registered")

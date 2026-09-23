"""Durable `--disable-service` set — the bridge between `ava start` and the watchdog.

`ava start --disable-service X` durably disables a service; the watchdog's 60s
healthcheck round would otherwise revive X (it derives its check list purely
from `machine_role`, with no knowledge of operator intent). This module persists
the operator's disabled set to `$AVA_HOME/disabled_services` so the watchdog consults
the same list and leaves X down until an operator `ava start` without the flag
re-enables it.

Service names are normalized to kebab-case on both write and compare: the
`--disable-service` value is the ServiceSpec session name (kebab, e.g.
`memory-indexer`), while the watchdog's check names are snake_case
(`memory_indexer`); without normalization a skip would silently miss.

Operator vs internal restart: a plain `ava start` (operator) rewrites the marker
from its `--disable-service` set — so re-enabling is just starting without the flag.
An internal restart (`ava cluster update` / recovery / `ava restart`) must NOT rewrite
it: those forward a transient skip (e.g. "leave frontend running") that is not a
durable disable, so they read the marker and union their transient skips for that
launch only (`resolve_launch_skip(persist=False)`).
"""

from __future__ import annotations

from pathlib import Path

from shared.paths import disabled_services_file


def _norm(name: str) -> str:
    """Canonical kebab-case form so `memory-indexer` (session name) and
    `memory_indexer` (watchdog check name) compare equal."""
    return name.strip().replace("_", "-")


def _read_names(path: Path) -> set[str]:
    """Parse a marker file into its normalized name set. Absent file -> empty."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return set()
    return {_norm(line) for line in text.splitlines() if line.strip()}


def read_skipped() -> set[str]:
    """The durably-skipped service names (normalized). Absent marker -> empty."""
    return _read_names(disabled_services_file())


def write_skipped(names: set[str]) -> None:
    """Record the durable skip set (normalized, sorted). Empty set writes an
    empty file — an explicit "nothing skipped", distinct from a never-written
    marker but read the same."""
    path = disabled_services_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(sorted(_norm(n) for n in names))
    path.write_text(body + "\n" if body else "")


def is_skipped(name: str, skipped: set[str]) -> bool:
    """Whether `name` (any case style) is in an already-read skip set."""
    return _norm(name) in skipped


def resolve_launch_skip(operator_skip: set[str], *, persist: bool) -> set[str]:
    """Compute the launch-time skip set and, on an operator start, record the
    durable intent for the watchdog.

    persist=True (operator `ava start`): the passed `--disable-service` set IS the
    durable intent — write it (re-enabling a previously-skipped service is just
    omitting it) and skip exactly it this launch.

    persist=False (internal restart — update / recovery / `ava restart`): do not
    touch the marker; skip the union of the persisted durable set and this
    restart's transient skips, for this launch only.
    """
    normalized = {_norm(n) for n in operator_skip}
    if persist:
        write_skipped(normalized)
        return normalized
    return read_skipped() | normalized

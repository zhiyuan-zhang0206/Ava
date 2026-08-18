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

The marker's file name changed once (`skipped_services` -> `disabled_services`);
`migrate_legacy_marker` carries a pre-rename file over and says so out loud, so
that rename cannot go on eating operator intent.
"""

from __future__ import annotations

from pathlib import Path

from shared.log import logger
from shared.paths import disabled_services_file

# The marker's basename, spelled out because `migrate_legacy_marker` works on a
# home passed to it rather than the settings-resolved one (reading the name off
# `disabled_services_file()` would mkdir THIS process's $AVA_HOME as a side
# effect). `test_marker_name_matches_paths_helper` pins the two spellings equal.
_MARKER_NAME = "disabled_services"

# The marker's pre-rename name. `skipped_services` was renamed to
# `disabled_services` by the affirmative-naming refactor (#315) with no
# migration, so an operator's durable intent recorded before it went silently
# unread — the services they disabled came back on and nothing said so.
# `migrate_legacy_marker` closes that; the archive suffix keeps a superseded
# legacy file on disk instead of deleting it.
_LEGACY_MARKER_NAME = "skipped_services"
_ARCHIVE_SUFFIX = ".superseded"


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


def _fmt(names: set[str]) -> str:
    """Render a name set for an operator-facing line; empty -> "(none)"."""
    return ", ".join(sorted(names)) if names else "(none)"


def migrate_legacy_marker(home: Path) -> str | None:
    """Carry a pre-rename `<home>/skipped_services` over to `disabled_services`.

    Returns a one-line summary of what it did (also logged at WARNING), or None
    when there is nothing to migrate — which is the steady state, so this is a
    no-op on every run after the first. Called by the converge step
    `cli.commands._converge._migrate_legacy_disabled_marker`, which runs on every
    `ava start` / `ava cluster update` / `ava converge` — before `resolve_launch_skip`,
    so the migrated intent lands in time for that same launch.

    Two cases, both by rename (never a rewrite — the file's bytes and mtime are
    the operator's own record):

    - `disabled_services` absent: the legacy file BECOMES it. The intent is
      honored from here on.
    - `disabled_services` present: it is authoritative — it is the name the
      current code writes, so it is the operator's later word, empty file
      included ("nothing disabled" is a real value here, not a missing one).
      The legacy file is NOT applied, but must not silently vanish either: it
      moves to `skipped_services.superseded` and the log names both sets, so an
      operator whose two files disagree can see which one was used.

    `home` is passed explicitly (not resolved from settings) so a caller acting
    on one cluster's home can never reach into another's.
    """
    live = home / _MARKER_NAME
    legacy = home / _LEGACY_MARKER_NAME
    if not legacy.exists():
        return None

    legacy_names = _read_names(legacy)
    if live.exists():
        archive = home / f"{_LEGACY_MARKER_NAME}{_ARCHIVE_SUFFIX}"
        live_names = _read_names(live)
        legacy.replace(archive)
        agreement = (
            "listed the same names"
            if live_names == legacy_names
            else f"listed {_fmt(legacy_names)} and was NOT applied"
        )
        summary = (
            f"{live.name} already exists and stays authoritative "
            f"(disabled: {_fmt(live_names)}); the pre-rename {legacy.name} {agreement} "
            f"— kept as {archive.name}"
        )
    else:
        legacy.replace(live)
        summary = (
            f"migrated the pre-rename {legacy.name} -> {live.name}; "
            f"durably disabled: {_fmt(legacy_names)}"
        )
    # Logged as well as returned: the caller prints it for the operator running the
    # command, the log is what survives for a start driven by `ava cluster update` or the
    # boot autostart. A one-time event, so it cannot become noise.
    logger.warning("[disabled-services] {summary}", summary=summary)
    return summary


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

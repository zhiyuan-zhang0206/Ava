"""Inspector metrics coverage verdicts — background log + signal (task #3869).

The inspector panel stopped rendering the read model's coverage verdicts
(user ruling 2026-09-17): "historical coverage is incomplete", a duration
precision note, a retained-source note — machinery, not user copy. The
backend keeps the record instead of the panel, one line per limit per read:

- limits expected by construction log at DEBUG: a window reaching back before
  the observation collection started (``historical_coverage_unknown``), a
  legacy archive whose per-day precision cannot be attributed per turn
  (``archive_precision_unattributed``, incl. retained sources). They are the
  panel's chronic noise and carry no signal.
- an UNEXPECTED limit — ``missing_turn_durations`` on a window inside the
  collection era (turns recorded without their durations is a live recorder
  gap, not history) — logs the ``inspect_metrics_coverage_gap`` WARNING event.
  Every read that still shows the gap emits it again, so an alert rule reads
  "still true" from the event stream and the episode ends when reads stop
  showing it.

Best-effort by contract: this runs on the statistics read path — nothing here
may fail the read.
"""

from __future__ import annotations

from datetime import datetime

from base.log import logger
from gateway.inspect.schemas import InspectMetricsMetadata

_FAMILIES = ("cost", "turns", "activity", "lifecycle")


def _historical(metadata: InspectMetricsMetadata, *, spawned_at: datetime) -> bool:
    """Mirror of the read model's ``historical`` flag (window reaches before
    the observation collection started). A missing window start reads
    historical — the quiet side."""
    if metadata.window_start is None:
        return True
    return max(metadata.window_start, spawned_at) < metadata.collection_started_at


def _conditions(
    metadata: InspectMetricsMetadata,
) -> list[tuple[str, str, str]]:
    """(family, condition, availability) triples for one read."""
    out: list[tuple[str, str, str]] = []
    for family in _FAMILIES:
        ev = getattr(metadata, family)
        if ev.availability != "observed" or ev.retained_unapplied_sources:
            if ev.reason == "missing_turn_durations":
                condition = "missing_turn_durations"
            elif ev.reason == "archive_precision_unattributed" or ev.retained_unapplied_sources:
                condition = "archive_precision_unattributed"
            else:
                condition = "historical_coverage_unknown"
            out.append((family, condition, ev.availability))
    return out


def _unexpected(condition: str, *, historical: bool) -> bool:
    return condition == "missing_turn_durations" and not historical


def note_inspect_metrics_coverage(
    agent_id: int,
    metadata: InspectMetricsMetadata,
    *,
    spawned_at: datetime,
) -> None:
    """Log every coverage limit of one read; the unexpected ones as the
    ``inspect_metrics_coverage_gap`` WARNING event. Never raises — see the
    module docstring."""
    try:
        historical = _historical(metadata, spawned_at=spawned_at)
        for family, condition, availability in _conditions(metadata):
            if _unexpected(condition, historical=historical):
                logger.warning(
                    "inspect metrics coverage gap",
                    event="inspect_metrics_coverage_gap",
                    agent_id=agent_id,
                    family=family,
                    condition=condition,
                    availability=availability,
                )
            else:
                logger.debug(
                    "inspect metrics coverage limit",
                    agent_id=agent_id,
                    family=family,
                    condition=condition,
                    availability=availability,
                )
    except Exception:
        logger.exception("inspect metrics coverage note failed", agent_id=agent_id)

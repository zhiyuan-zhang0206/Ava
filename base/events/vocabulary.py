"""Event vocabulary: categories, tiers, ``EventSpec`` and its builders.

Every domain module under ``base/events/declarations/`` declares its events with
these; ``base/events/loader.py`` merges the modules and ``base/events/contract.py``
re-exports the vocabulary and derives the views.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

Category = Literal["audit", "telemetry", "log"]
EventTier = Literal["business", "anomaly", "observation", "noise"]

# Event tiers control the human-facing event stream, independently from the
# category that controls event-class access semantics:
#
# - business: an audit fact a human normally performed or requested;
# - anomaly: a warning/error or a problem-shaped signal needing attention;
# - observation: useful runtime progress that is folded by default; and
# - noise: implementation-detail telemetry retained for debugging.
#
# ``tier_for`` below applies the row-level priority: warning+ levels always
# win, then audit category, then the declared name. This lets ``status_change``
# remain one registry entry while its audit rows are business and its telemetry
# rows are noise.

# The ops-monitor bucket grid (the Insights Ops panel): 60s buckets on a fixed
# origin, shared by the LGTM reader (gateway/cluster/ops_series.py) and the
# frontend's expectation that bucket boundaries never shift with the query
# time. OPS_BUCKET_S is the finest window step; coarser windows are multiples.
OPS_BUCKET_S = 60
OPS_GRID_ORIGIN = datetime(2000, 1, 1, tzinfo=UTC)

# The LLM failure family — one declaration; the ops panels / rollups that used
# to carry three hand-copied `_LLM_ERROR_EVENTS` tuples read `family_events`.
LLM_ERROR_FAMILY = "LLM_ERROR"


@dataclass(frozen=True)
class EventSpec:
    """One declared event: name x category x payload x destination.

    ``extra_categories``: a name that genuinely carries more than one category
    (status_change: the loguru side emits telemetry, audit_events emits audit).

    ``tier``: the default human-facing event tier. ``tier_for`` may override
    it for an anomaly level or an audit row. ``destination``: ``"events"``
    (default — carried on the event stream) or
    ``"file"`` (log-file only, e.g. ``node_enter`` after PR #1758's sink
    filter). ``family`` groups events the ops panels / rollups treat as one
    family (e.g. LLM_ERROR). ``doc`` is the one-line registry.md description.

    ``site``: where a producer the static ``event=`` literal scan cannot see
    (positional emit, dynamic name, SQL write) emits this name. A non-empty
    ``site`` is the producer evidence ``tests/test_lint_event_kinds.py`` accepts
    for the name, so the exemption lives with the declaration. ``retired``: a
    historical name kept readable for existing rows; it must have no producer.
    """

    name: str
    category: Category
    tier: EventTier
    extra_categories: frozenset[Category] = frozenset()
    payload: Any | None = None
    destination: Literal["events", "file"] = "events"
    family: str | None = None
    doc: str = ""
    site: str = ""
    retired: bool = False


def audit_event(
    name: str,
    doc: str,
    *,
    payload: Any | None = None,
    tier: EventTier = "business",
    site: str = "",
    retired: bool = False,
) -> EventSpec:
    return EventSpec(
        name=name,
        category="audit",
        tier=tier,
        payload=payload,
        doc=doc,
        site=site,
        retired=retired,
    )


def telemetry_audit_event(
    name: str,
    doc: str,
    *,
    payload: Any | None = None,
    site: str = "",
    retired: bool = False,
) -> EventSpec:
    """A name that genuinely carries both categories (status_change: the
    loguru side emits telemetry, audit_events emits audit)."""
    return EventSpec(
        name=name,
        category="telemetry",
        tier="noise",
        extra_categories=frozenset({"audit"}),
        payload=payload,
        doc=doc,
        site=site,
        retired=retired,
    )


def telemetry_event(
    name: str,
    doc: str,
    *,
    payload: Any | None = None,
    family: str | None = None,
    destination: Literal["events", "file"] = "events",
    tier: EventTier = "observation",
    site: str = "",
    retired: bool = False,
) -> EventSpec:
    return EventSpec(
        name=name,
        category="telemetry",
        tier=tier,
        payload=payload,
        family=family,
        destination=destination,
        doc=doc,
        site=site,
        retired=retired,
    )

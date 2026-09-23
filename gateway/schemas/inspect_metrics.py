"""Evidence and window semantics for the persisted Inspector read model."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MetricEvidence(BaseModel):
    """Availability of observed data, never a claim that every event was collected.

    A successful source scan cannot prove that an upstream producer lost nothing.
    Missing historical data and missing exact duration observations remain explicit.
    """

    model_config = ConfigDict(frozen=True)

    availability: Literal["observed", "partial", "unavailable"]
    sources: list[str]
    retained_unapplied_sources: list[str] = Field(default_factory=list)
    reason: (
        Literal[
            "historical_coverage_unknown",
            "missing_turn_durations",
            "archive_precision_unattributed",
        ]
        | None
    ) = None
    duration_precision: Literal["exact", "one_second_buckets", "mixed"] | None = None


class InspectMetricsMetadata(BaseModel):
    """One pinned query window and the evidence behind each statistics family.

    ``last_observed_at`` is the newest persisted observation, not a completeness
    watermark. ``sampled_at`` identifies the DB read, including when cached.
    """

    model_config = ConfigDict(frozen=True)

    collection: Literal["observed"] = "observed"
    window_start: datetime | None
    window_end: datetime
    sampled_at: datetime
    collection_started_at: datetime
    last_observed_at: datetime | None
    cost: MetricEvidence
    turns: MetricEvidence
    activity: MetricEvidence
    lifecycle: MetricEvidence

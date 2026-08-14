"""Frontend telemetry ingestion — request schema for POST /api/frontend-telemetry.

The browser telemetry module (`frontend/src/lib/telemetry.ts`) batches tracked
interactions and posts them here. Validation is strict on shape (fail fast:
a malformed batch is a bug in our own client) and lenient on content (the
semantic allowlist — which elements exist — lives in the frontend module, the
single producer). No attribute may carry free text: `value` is a sanitized
scalar rendering (bool / number / ≤64-char string) produced by the client,
never raw input content.
"""

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)


# One tracked interaction as the client saw it. `ts` is the client-side
# wall-clock (ms epoch) and is advisory only — the server stamps rows with
# its own clock so a buggy or offline client cannot inject timestamps.
# `key` / `value` exist for setting-change events only; other events omit
# them (None).
class FrontendInteractionIn(BaseModel):
    """One tracked interaction from the browser."""

    model_config = ConfigDict(frozen=True)

    page: str = Field(pattern=r"^[a-z0-9/_-]{1,64}$")
    element: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    key: str | None = Field(default=None, pattern=r"^[a-z0-9._-]{1,128}$")
    value: str | None = Field(default=None, max_length=64)
    ts: int | None = Field(default=None, ge=0)


class FrontendTelemetryBatch(BaseModel):
    """POST /api/frontend-telemetry body — one tab's buffered interactions."""

    model_config = ConfigDict(frozen=True)

    session_id: str = Field(pattern=r"^[0-9a-f-]{8,64}$")
    events: list[FrontendInteractionIn] = Field(min_length=1, max_length=200)

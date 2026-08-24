"""Gateway RFC 9457-style error response schema."""

from pydantic import BaseModel

from shared.agents import ErrorReason


class ErrorEnvelope(BaseModel):
    """The JSON body emitted for every gateway API error response."""

    type: str = "about:blank"
    code: str
    status: int
    detail: str
    retryable: bool
    trace_id: str
    reason: ErrorReason | None = None

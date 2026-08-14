"""agent shell capture.

Split out of the former monolithic ops/schemas.py; FastAPI registers these
unchanged, so the OpenAPI codegen is byte-identical to the wire before.
"""

from pydantic import (
    BaseModel,
    ConfigDict,
)


class ShellCaptureResponse(BaseModel):
    """GET /api/agents/{id}/shell/{sid} response — a recent tail of one
    persistent-shell session's terminal output.

    `lines` is the session's most recent output (up to the last 200 lines,
    history that has scrolled past included), one string per line with no
    trailing newline. `session_name` is the resolved session name the
    capture came from. Backs the shell monitor page, which polls it while open;
    the session must be live (404 otherwise)."""

    model_config = ConfigDict(frozen=True)

    agent_id: int
    session_id: int
    session_name: str
    lines: list[str]

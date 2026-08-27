"""agent shell capture.

Split out of the former monolithic ops/schemas.py; FastAPI registers these
unchanged, so the OpenAPI codegen is byte-identical to the wire before.
"""

from datetime import datetime

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
    the session must be live (404 otherwise).

    `created_at` / `uptime_seconds` come from the runner's session record
    (the launch epoch + probe-time uptime); `expires_at` is the gateway-owned
    TTL deadline from `agent_shell_ttls` — None when the session has no TTL.
    Together they let the monitor page's title bar render runtime + TTL
    without a second probe."""

    model_config = ConfigDict(frozen=True)

    agent_id: int
    session_id: int
    session_name: str
    lines: list[str]
    created_at: datetime | None = None
    uptime_seconds: int = 0
    expires_at: datetime | None = None

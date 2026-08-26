"""Shell operations: one-shot run, background run with completion notice,
and persistent sessions."""

__all_for_ava__ = ["run", "run_background", "sessions"]

import subprocess
from pathlib import Path
from typing import NamedTuple

from ava import _boot
from ava.security import scan_content
from shared.paths import workspace_dir
from shared.platform import CREATE_NO_WINDOW

from . import _background
from . import sessions as sessions

# Back-compat re-exports for `ava.shell.kill_all` / `ava.shell.send` style
# callers (tests, incl. the loop test's monkeypatch target). Not in `__all_for_ava__`
# so `help(ava.shell)` only advertises `run` and the `sessions` submodule.
from .sessions import (
    capture as capture,
)
from .sessions import (
    kill as kill,
)
from .sessions import (
    kill_all as kill_all,
)
from .sessions import (
    list as list,
)
from .sessions import (
    new as new,
)
from .sessions import (
    send as send,
)
from .sessions import (
    send_keys as send_keys,
)


def run(
    cmd: str,
    *,
    cwd: str | None = None,
    timeout: float = 30.0,
) -> str:
    """Run a shell command and return its stdout. Non-zero exit does not
    raise; exceeding `timeout` seconds kills the command and raises
    `subprocess.TimeoutExpired`. `cwd` defaults to your workspace.

    For commands expected to outlive the timeout, use `run_background`
    instead — it reports back when the command finishes."""
    # stdout flows straight into the agent's context, which makes this an
    # ingestion surface like `files.read` and `web.fetch` — and the widest one,
    # since any fetcher a command happens to invoke lands here. The scan returns
    # the text byte-for-byte and only records findings, so output is unaffected.
    # Same baseline as `ava.files._resolve`: with no explicit cwd and no
    # plugin-tracked cwd layered on top, commands run in the agent's
    # workspace; pre-identity (test / dev REPL without a bootstrap) falls
    # back to $HOME — the same base files._resolve uses, never the process
    # cwd, so the two surfaces agree in every state.
    if cwd is None:
        aid = _boot.agent_id()
        # agent id is typed int but is None until a bootstrap establishes it.
        cwd = str(workspace_dir(aid)) if aid is not None else str(Path.home())  # pyright: ignore[reportUnnecessaryComparison]
    completed = subprocess.run(  # noqa: S602
        cmd,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        creationflags=CREATE_NO_WINDOW,
    )
    # The command itself is deliberately not part of `source`: it routinely
    # carries credentials (a curl with an Authorization header), and the
    # findings file is not a place to copy them into.
    return scan_content(completed.stdout, source="shell.run")


class BackgroundRun(NamedTuple):
    """Handle returned by `run_background`: the session the command runs in and
    the log file its output streams to."""

    session_id: int
    output_path: str


def run_background(
    cmd: str,
    *,
    name: str,
    cwd: str | None = None,
    keep: bool = False,
    ttl: float | None = None,
) -> BackgroundRun:
    """Run a long shell command in the background; you get a message when it
    finishes — no polling. Returns immediately.

    The command runs in a fresh persistent session, stdout+stderr streaming to
    `output_path` (`.shell_logs/<session_id>_<name>.log` in your workspace) —
    read it any time to check progress. The completion message carries the
    exit code, the log path, and the output tail; the session then closes
    itself unless `keep=True`. While running it is an ordinary session.

    Use this for one-shot long tasks (builds, test suites, downloads);
    interactive programs belong in `sessions.new` + `send`.

    Args:
        cmd: must be a single line — join steps with `;` or `&&`, or point at
            a script file.
        name: a lowercase slug like `"build"`.
        cwd: defaults to your workspace.
        ttl: optional hard lifetime in seconds, counted from creation — the
            session is force-killed once it elapses, with no idle/activity
            renewal. Omit for a session that is never auto-reclaimed;
            `keep=True` does not extend or disable it.
    """
    if not cmd.strip():
        raise ValueError("cmd cannot be empty")
    aid = _boot.agent_id()
    if cwd is None:
        cwd = str(workspace_dir(aid))
    session_id, _full = sessions._create_session(name, cwd=cwd, ttl=sessions._validate_ttl(ttl))
    output_path = _background.allocate_output_path(session_id, name)
    line = _background.notified_line(
        cmd,
        agent_id=aid,
        label=f"Background command '{name}'",
        source=f"shell:{session_id}",
        output_path=output_path,
        keep=keep,
    )
    sessions.send(session_id, line)
    return BackgroundRun(session_id=session_id, output_path=str(output_path))

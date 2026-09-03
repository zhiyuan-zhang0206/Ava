"""Shared plumbing for background commands that report back on completion.

`ava.shell.run_background` and `ava.watcher._spawn` share one shape: run a
command inside a persistent session with its output teed to a per-agent log
file and the session capture, then deliver a completion notice through the
`ava agents send` CLI (exit code + log path + output tail). This module builds
that command line and allocates the log files; the session mechanics stay in
`sessions.py`.

The session shell is Bash, so the generated pipeline captures the command's
exit status from `PIPESTATUS[0]` immediately after `tee`. `pipefail` would
both let a failed `tee` replace the command status and mutate a kept
interactive shell's state.

The notice is sent from the shell level (not from inside the command's own
process) so it fires on every exit path — including a SIGKILL'd or OOM'd child
whose in-process cleanup never runs. `&& exit` closes the session only after
the notice is delivered: if the send fails, the session survives as the
post-mortem site and the agent finds it via `sessions.list()`.
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

import ava._boot
from shared.paths import workspace_dir

# Background command output is teed to `.shell_logs/` inside the agent's
# workspace and the session capture. The log survives session close and
# scrollback limits — and lives at a predictable spot: the workspace is the
# default base of the agent's file and shell tools, so
# `.shell_logs/<sid>_<name>.log` is readable by relative path, and the path is
# reconstructable from the session id alone. Sibling of `.exec_output/`
# (oversized exec-output overflow), with its own ring so the two cannot evict
# each other.
_OUTPUT_DIRNAME = ".shell_logs"
# Ring: keep the most recent N logs per workspace, prune older ones so the dir
# does not grow unbounded across a long-lived agent.
_OUTPUT_KEEP = 20

# Characters that stay shell-active inside a double-quoted string.
_DQUOTE_UNSAFE = re.compile(r'[$`"\\]')


def output_dir() -> Path:
    """This agent's background-run log dir (`.shell_logs/` in its workspace)."""
    return workspace_dir(ava._boot.agent_id()) / _OUTPUT_DIRNAME


def allocate_output_path(session_id: int, name: str) -> Path:
    """Create (touch) and return the log file for one background run, pruning
    the workspace's oldest logs beyond the ring size. Touching the file up
    front makes the path immediately readable (progress can be tailed
    mid-run).

    A log whose session is still alive is never evicted, even past the ring
    size: a long-lived watcher's log keeps its spawn-time mtime for its whole
    lifetime (a time watcher sleeps between fires and only writes its
    schedule-state announcement lines — template v3, see shared/watcher.py),
    so mtime-LRU alone would evict it while its process still holds the fd —
    orphaning the output and breaking the exit notice's tail. Live sessions
    outrank the ring cap; the cap re-applies once they end.
    """
    from ava.shell import sessions as _sessions

    d = output_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session_id}_{name}.log"
    path.touch()
    live = set(_sessions.list())
    existing = sorted(d.glob("*.log"), key=lambda p: p.stat().st_mtime)
    for old in existing[:-_OUTPUT_KEEP]:
        sid = old.name.split("_", 1)[0]
        if sid.isdigit() and int(sid) in live:
            continue
        old.unlink(missing_ok=True)
    return path


def cli_path() -> Path:
    """Path of the `ava` CLI next to the running interpreter (installed into the
    venv's bin by `uv sync`). The session's own PATH is not trusted — a login
    shell re-initializes it, so the generated line must carry an absolute path."""
    p = Path(sys.executable).with_name("ava")
    if not p.exists():
        raise RuntimeError(
            f"ava CLI not found at {p} — background completion notices need the "
            "CLI installed alongside the interpreter (uv sync installs it)"
        )
    return p


def notified_line(
    cmd: str,
    *,
    agent_id: int,
    label: str,
    source: str,
    output_path: Path,
    keep: bool,
) -> str:
    """Build the shell line: run `cmd` in a subshell, tee stdout+stderr to
    `output_path` and the session capture, then deliver the completion notice,
    then close the session unless `keep`.

    `cmd` must be a single line: the line is typed into the session's shell,
    so an embedded newline would submit a partial command. The subshell makes
    the pipeline cover a compound `cmd` (`a; b`) as one unit;
    `${PIPESTATUS[0]}` captures the subshell's exit code immediately before any
    other command can reset it. `pipefail` is unsuitable because a failed
    `tee` could replace that exit code and it would mutate a kept interactive
    shell's state. `label` and `source` are caller-controlled literals (no user
    text), and the double-quoted notice only expands `${_ec}`.
    """
    if "\n" in cmd or "\r" in cmd:
        raise ValueError(
            "cmd must be a single line — join steps with ';' or '&&', or write a "
            "script file and run that"
        )
    # The notice rides inside a double-quoted shell string where only ${_ec} may
    # expand. Callers build label/path from validated slugs and framework paths;
    # this re-declares that invariant here so a future caller cannot silently
    # reopen expansion via $ ` " or backslash.
    for part, what in ((label, "label"), (str(output_path), "output_path")):
        if _DQUOTE_UNSAFE.search(part):
            raise ValueError(f"{what} contains shell-active characters: {part!r}")
    q_path = shlex.quote(str(output_path))
    notice = f"{label} exited with code ${{_ec}}. Full output at {output_path}."
    line = (
        f"( {cmd} ) 2>&1 | tee {q_path}; _ec=${{PIPESTATUS[0]}}; "
        f'{shlex.quote(str(cli_path()))} agents send {agent_id} "{notice}" '
        f"--source {source} --tail-file {q_path}"
    )
    if not keep:
        line += "; exit $_ec"
    return line

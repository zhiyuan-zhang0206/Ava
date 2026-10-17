# meiva:link meiva/shell/docs/shell.meiva.okf.md
from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

import meiva.sdk_surface.agent_identity
from base.paths import workspace_dir

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
OUTPUT_KEEP = 20

# Characters that stay shell-active inside a double-quoted string.
_DQUOTE_UNSAFE = re.compile(r'[$`"\\]')
_NOTIFY_POLICIES = ("always",)


def output_dir() -> Path:
    """This agent's background-run log dir (`.shell_logs/` in its workspace)."""
    return workspace_dir(meiva.sdk_surface.agent_identity.require_agent_id()) / _OUTPUT_DIRNAME


def allocate_output_path(session_id: int, name: str) -> Path:
    """Create (touch) and return the log file for one background run, pruning
    the workspace's oldest logs beyond the ring size. Touching the file up
    front makes the path immediately readable (progress can be tailed
    mid-run).

    A log whose session is still alive is never evicted, even past the ring
    size: a long-lived watcher's log keeps its spawn-time mtime for its whole
    lifetime (a time watcher sleeps between fires and only writes its
    schedule-state announcement lines — template v3, see base/daemon/schedules/watcher.py),
    so mtime-LRU alone would evict it while its process still holds the fd —
    orphaning the output and breaking the exit notice's tail. Live sessions
    outrank the ring cap; the cap re-applies once they end.
    """
    from meiva.shell import sessions as _sessions

    d = output_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session_id}_{name}.log"
    path.touch()
    live = set(_sessions.list())
    existing = sorted(d.glob("*.log"), key=lambda p: p.stat().st_mtime)
    for old in existing[:-OUTPUT_KEEP]:
        sid = old.name.split("_", 1)[0]
        if sid.isdigit() and int(sid) in live:
            continue
        old.unlink(missing_ok=True)
        old.with_suffix(".sh").unlink(missing_ok=True)
    return path


def cli_path() -> Path:
    """Path of the `meiva` CLI next to the running interpreter (installed into the
    venv's bin by `uv sync`). The session's own PATH is not trusted — a login
    shell re-initializes it, so the generated line must carry an absolute path."""
    p = Path(sys.executable).with_name("meiva")
    if not p.exists():
        raise RuntimeError(
            f"meiva CLI not found at {p} — background completion notices need the "
            "CLI installed alongside the interpreter (uv sync installs it)"
        )
    return p


def validate_notify(notify: str | None) -> str | None:
    """Validate an explicit per-run policy; None delegates to the agent policy."""
    if notify is not None and notify not in _NOTIFY_POLICIES:
        raise ValueError(f"notify must be one of {list(_NOTIFY_POLICIES)}, got {notify!r}")
    return notify


def write_launch_file(line: str, path: Path) -> str:
    # meiva:link meiva/shell/docs/shell.meiva.okf.md
    quoted = shlex.quote(str(path))
    path.write_text(f"command rm -f -- {quoted}\n{line}\n")
    return f". {quoted}"


def notified_line(
    cmd: str,
    *,
    agent_id: int,
    label: str,
    source: str,
    output_path: Path,
    keep: bool,
    notify: str | None = None,
) -> str:
    # meiva:link meiva/shell/docs/shell.meiva.okf.md
    notify = validate_notify(notify)
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
    notice = f"{label} finished. Full output at {output_path}."
    send_notice = (
        f'{shlex.quote(str(cli_path()))} agents send {agent_id} "{notice}" '
        f"--source {source} --tail-file {q_path}"
    )
    line = f"exec 3> >(tee {q_path}); ( {cmd} ) >&3 2>&1; _ec=$?; exec 3>&-; "
    line += send_notice if notify == "always" else f"{send_notice} --completion"
    if not keep:
        line += "; exit $_ec"
    return line

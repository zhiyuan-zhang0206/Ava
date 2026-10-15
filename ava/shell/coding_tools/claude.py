"""Launch, inspect, or stop a Claude Code session in a persistent shell.

A supervised worker (the default) keeps the two-file collaboration: a task
file, a work file, and a session the launcher's owner follows with the skill's
``watch_work.py``. An interactive prompt launch (``prompt``) starts Claude Code
without task/work files and delivers the prompt as its first message; an Ava
agent that wants Claude Code to impersonate it simply spawns a session and
sends it a message with the impersonation instructions.

The ``ava-guide.external-agents`` skill's ``spawn_claude.py`` is the command-line
entry; it passes its own skill directory, whose ``references/`` holds the
collaboration contract.
"""

from __future__ import annotations

import shlex
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

from ava.shell import sessions
from base.sessions import coding_session_owner

from ._claude_checks import (
    login_marker,
    pretrust,
    send_bootstrap,
    session_exists,
    wait_for_ready,
)
from ._common import cancel as _cancel_generation
from ._common import (
    init_file,
    kill_session_after_failed_launch,
    worker_bootstrap,
)
from ._common import resolve_dir as resolve_dir
from ._common import resolve_file as resolve_file
from ._common import session_uuid as session_uuid
from ._common import status as _owner_status
from ._first_run import preset_claude_first_run

DEFAULT_TTL_SECONDS = 24 * 3600


def claude_command(
    workspace: Path,
    caller_instance: str | None = None,
    *,
    failure_marker: Path | None = None,
    claude_session: str | None = None,
    resume: bool = False,
) -> str:
    """The launch line: checks for the executable and login, then execs Claude Code.

    ``claude_session`` pins the session id up front (``--session-id``) so the
    launch can print it; with ``resume`` the same id reopens that recorded
    session (``--resume``) instead.
    """
    from base.agents.messages.external_caller import launch_caller_assignment

    session_flag = ""
    if claude_session is not None:
        option = "--resume" if resume else "--session-id"
        session_flag = f" {option} {shlex.quote(claude_session)}"
    mark_failure = (
        f"printf '%s\\n' 'claude executable not found' > {shlex.quote(failure_marker.as_posix())}; "
        if failure_marker is not None
        else ""
    )
    # A signed-out CLI still renders a ready panel and only fails on its first
    # turn, so ask it directly before exec (exit 1 when logged out).
    mark_logout = (
        f"printf '%s\\n' 'not logged in' > {shlex.quote(login_marker(failure_marker).as_posix())}; "
        if failure_marker is not None
        else ""
    )
    return (
        f"cd {shlex.quote(workspace.as_posix())} && "
        "unset ANTHROPIC_API_KEY && "
        "claude_bin=$(command -v claude); "
        'if [ -z "$claude_bin" ]; then claude_bin="$HOME/.local/bin/claude"; fi; '
        'if [ ! -x "$claude_bin" ]; then '
        f"{mark_failure}"
        "printf '%s\\n' 'error: claude executable not found in PATH or "
        "$HOME/.local/bin/claude' >&2; exit 127; fi; "
        'if ! "$claude_bin" auth status >/dev/null 2>&1; then '
        f"{mark_logout}"
        "printf '%s\\n' 'error: claude is not logged in; run claude auth login' >&2; exit 126; fi; "
        f"{launch_caller_assignment('claude_code', caller_instance)}"
        f'exec "$claude_bin" --dangerously-skip-permissions{session_flag} || exit $?'
    )


def _print_owner(owner: coding_session_owner.CodingSessionOwner) -> None:
    print(f"status={owner.status}")
    if owner.generation is not None:
        print(f"generation={owner.generation}")
    if owner.owner_agent_id is not None:
        print(f"owner_agent_id={owner.owner_agent_id}")
    if owner.session_id is not None:
        print(f"session_id={owner.session_id}")
    if owner.session_name is not None:
        print(f"session_name={owner.session_name}")
    if owner.state_dir is not None:
        print(f"state_dir={owner.state_dir}")
    if owner.tasks_file is not None:
        print(f"tasks_file={owner.tasks_file}")
    if owner.work_file is not None:
        print(f"work_file={owner.work_file}")


def status(workspace: Path) -> int:
    """Print the workspace's recorded generations."""
    return _owner_status(
        coding_session_owner.canonical_key(workspace, tool="claude"),
        _print_owner,
    )


def cancel(workspace: Path, generation: str) -> int:
    """Stop and terminalize exactly this generation."""
    return _cancel_generation(
        coding_session_owner.canonical_key(workspace, tool="claude"),
        generation,
        _print_owner,
    )


def run_supervised_launch(
    workspace: Path,
    tasks_file: Path | None,
    work_file: Path | None,
    ttl_seconds: float,
    caller_instance: str | None,
    contract: Path,
    claude_session: str,
    *,
    resume: bool,
    prompt: str | None = None,
    check_session_exists: Callable[[str], bool] | None = None,
) -> int:
    if tasks_file is not None and work_file is not None:
        init_file(tasks_file, "")
        init_file(work_file, "STATUS: WORKING\n\n## Log\n\n")
    pretrust(workspace)

    session_name = f"claude-{workspace.name}"
    if (check_session_exists or session_exists)(session_name):
        print(
            f"session '{session_name}' already exists. session id: see `ava.shell.sessions.list()`"
        )
        return 1

    sid = sessions.new(name=session_name, ttl=ttl_seconds)
    try:
        with tempfile.TemporaryDirectory(prefix="ava-claude-launch-") as marker_dir:
            marker = Path(marker_dir) / "missing-claude"
            sessions.send(
                sid,
                claude_command(
                    workspace,
                    caller_instance,
                    failure_marker=marker,
                    claude_session=claude_session,
                    resume=resume,
                ),
            )
            print(f"+ persistent shell session: {sid} ({session_name})")
            wait_for_ready(sid, failure_marker=marker, resumed=resume)

        if prompt is not None:
            send_bootstrap(sid, prompt)
        else:
            assert tasks_file is not None and work_file is not None  # noqa: S101
            sessions.send(
                sid, worker_bootstrap(contract, workspace, tasks_file, work_file, resumed=resume)
            )
    except BaseException:
        kill_session_after_failed_launch(sid)
        raise

    print(f"ready. name={session_name}  workspace={workspace}")
    print(f"session_id={sid}")
    if tasks_file is not None and work_file is not None:
        print(f"tasks_file={tasks_file}")
        print(f"work_file={work_file}")
    print(f"claude_session={claude_session}")
    if tasks_file is not None and work_file is not None:
        print(
            "NOTE: for xhigh effort on >10 files, consider STALL_SECONDS=1200+ "
            "in watch_work.py to avoid false stall alerts."
        )
    return 0


def launch(
    workspace: Path,
    tasks_file: Path | None,
    work_file: Path | None,
    ttl_seconds: float,
    caller_instance: str | None = None,
    *,
    skill_dir: Path,
    resume: str | None = None,
    prompt: str | None = None,
    preset_first_run: bool = True,
    check_session_exists: Callable[[str], bool] | None = None,
) -> int:
    """Launch a Claude Code session in a persistent shell.

    When ``prompt`` is given, sends that initial prompt without creating
    task/work files. Otherwise launches a supervised worker with ``tasks_file``
    and ``work_file``.
    """
    from base.agents.messages.external_caller import launch_caller_assignment

    if prompt is not None:
        if not prompt.strip():
            raise ValueError("a prompt launch needs a non-empty prompt")
        if tasks_file is not None or work_file is not None:
            raise ValueError("a prompt launch does not use task or work files")
    elif tasks_file is None or work_file is None:
        raise ValueError("a supervised launch needs its task and work files")

    launch_caller_assignment("claude_code", caller_instance)
    claude_session = resume or str(uuid.uuid4())
    if preset_first_run:
        preset_claude_first_run()
    return run_supervised_launch(
        workspace,
        tasks_file,
        work_file,
        ttl_seconds,
        caller_instance,
        skill_dir / "references" / "collaboration_protocol.md",
        claude_session,
        resume=resume is not None,
        prompt=prompt,
        check_session_exists=check_session_exists,
    )

"""Launch, inspect, or stop a Claude Code session in a persistent shell.

A supervised worker (the default) keeps the two-file collaboration: a task
file, a work file, and a session the launcher's owner follows with the skill's
``watch_work.py``. A takeover (``impersonation_name``) runs file- and
supervisor-less with its briefing inlined in the launch message, under an
owner generation of its own keyed by ``(cluster, workspace, claude)``; its relay
starts with the session via the skill's bundled ava-relay plugin (resident
mode) unless the executor-armed Monitor flow is requested.

The ``ava-use-other-agents`` skill's ``spawn_claude.py`` is the command-line
entry; it passes its own skill directory, whose ``references/`` holds the
collaboration contract (and locates the impersonator guide) and whose
``scripts/`` holds the bundled relay plugin.
"""

from __future__ import annotations

import shlex
import sys
import tempfile
import uuid
from pathlib import Path

import ava
from base.sessions import coding_session_owner

from ._claude_checks import (
    _bootstrap_count,
    _generation_relay_stub,
    _login_marker,
    _pretrust,
    _send_bootstrap,
    _session_exists,
    _verify_start_receipt,
    _wait_for_ready,
)
from ._common import cancel as _cancel_generation
from ._common import (
    impersonator_guide,
    init_file,
    kill_session_after_failed_launch,
    new_generation,
    terminate_generation_after_failed_launch,
    worker_bootstrap,
)
from ._common import resolve_dir as resolve_dir
from ._common import resolve_file as resolve_file
from ._common import session_uuid as session_uuid
from ._common import status as _owner_status
from ._first_run import _preset_claude_first_run

DEFAULT_TTL_SECONDS = 24 * 3600


def _claude_command(
    workspace: Path,
    caller_instance: str | None = None,
    *,
    failure_marker: Path | None = None,
    relay_stub: Path | None = None,
    relay_plugin_dir: Path | None = None,
    claude_session: str | None = None,
    resume: bool = False,
) -> str:
    """The launch line: checks for the executable and login, then execs Claude Code.

    ``claude_session`` pins the session id up front (``--session-id``) so the
    launch can print it; with ``resume`` the same id reopens that recorded
    session (``--resume``) instead.
    """
    from base.agents.messages.external_caller import launch_caller_assignment

    resident = ""
    plugin_flag = ""
    if (relay_stub is None) != (relay_plugin_dir is None):
        raise ValueError("the resident relay needs both its stub path and its plugin dir")
    if relay_stub is not None and relay_plugin_dir is not None:
        stub = relay_stub
        resident = (
            f"export AVA_IMPERSONATION_RELAY_STUB={shlex.quote(stub.as_posix())} "
            f"AVA_IMPERSONATION_RELAY_PY={shlex.quote(sys.executable)} && "
        )
        plugin_flag = f" --plugin-dir {shlex.quote(relay_plugin_dir.as_posix())}"
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
        f"printf '%s\\n' 'not logged in' > {shlex.quote(_login_marker(failure_marker).as_posix())}; "
        if failure_marker is not None
        else ""
    )
    return (
        f"cd {shlex.quote(workspace.as_posix())} && "
        "unset ANTHROPIC_API_KEY && "
        f"{resident}"
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
        f'exec "$claude_bin" --dangerously-skip-permissions{plugin_flag}{session_flag} || exit $?'
    )


def _takeover_bootstrap_message(
    agent_id: int, name: str, brief: str, guide: Path, *, relay_resident: bool
) -> str:
    """Inline the briefing; a takeover reads no task or work file."""
    from ava.impersonation.launch import bootstrap_message

    return bootstrap_message(agent_id, name, "claude", brief, guide, relay_resident=relay_resident)


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
    """Print the workspace's takeover generations (supervised launches are not registered)."""
    return _owner_status(
        coding_session_owner.canonical_key(workspace, tool="claude"),
        _print_owner,
    )


def cancel(workspace: Path, generation: str) -> int:
    """Stop and terminalize exactly this takeover generation."""
    return _cancel_generation(
        coding_session_owner.canonical_key(workspace, tool="claude"),
        generation,
        _print_owner,
    )


def _run_supervised_launch(
    workspace: Path,
    tasks_file: Path,
    work_file: Path,
    ttl_seconds: float,
    caller_instance: str | None,
    contract: Path,
    claude_session: str,
    *,
    resume: bool,
) -> int:
    init_file(tasks_file, "")
    init_file(work_file, "STATUS: WORKING\n\n## Log\n\n")
    _pretrust(workspace)

    session_name = f"claude-{workspace.name}"
    if _session_exists(session_name):
        print(
            f"session '{session_name}' already exists. session id: see `ava.shell.sessions.list()`"
        )
        return 1

    # Create a persistent shell session visible in the Inspect panel. TTL is
    # mandatory (2026-08-27 ruling); the default 24h is a generous cap for a
    # coding session. A session reclaimed before its work is done is reopened
    # with --resume and the claude_session printed below.
    sid = ava.shell.sessions.new(name=session_name, ttl=ttl_seconds)
    try:
        with tempfile.TemporaryDirectory(prefix="ava-claude-launch-") as marker_dir:
            marker = Path(marker_dir) / "missing-claude"
            ava.shell.sessions.send(
                sid,
                _claude_command(
                    workspace,
                    caller_instance,
                    failure_marker=marker,
                    claude_session=claude_session,
                    resume=resume,
                ),
            )
            print(f"+ persistent shell session: {sid} ({session_name})")
            _wait_for_ready(sid, failure_marker=marker, resumed=resume)

        ava.shell.sessions.send(
            sid, worker_bootstrap(contract, workspace, tasks_file, work_file, resumed=resume)
        )
    except BaseException:
        kill_session_after_failed_launch(sid)
        raise

    print(f"ready. name={session_name}  workspace={workspace}")
    print(f"session_id={sid}")
    print(f"tasks_file={tasks_file}")
    print(f"work_file={work_file}")
    print(f"claude_session={claude_session}")
    print(
        "NOTE: for xhigh effort on >10 files, consider STALL_SECONDS=1200+ "
        "in watch_work.py to avoid false stall alerts."
    )
    return 0


def _run_takeover_launch(
    workspace: Path,
    takeover_name: str,
    takeover_brief: str,
    ttl_seconds: float,
    caller_instance: str | None,
    skill_dir: Path,
    claude_session: str,
    *,
    resume: bool = False,
    relay_resident: bool = True,
    relay_plugin_dir: Path | None = None,
) -> int:
    plugin_dir: Path | None = None
    if relay_resident:
        candidate = relay_plugin_dir or skill_dir / "scripts" / "ava-relay"
        if not candidate.is_dir():
            raise RuntimeError(
                f"the relay plugin directory is missing: {candidate}; "
                "launch with --no-relay-resident to use the executor-armed flow"
            )
        plugin_dir = candidate.resolve()
    key = coding_session_owner.canonical_key(workspace, tool="claude")
    owner = new_generation(key, tasks_file=None, work_file=None, ttl_seconds=ttl_seconds)
    if owner.generation is None or owner.expected_suffix is None or owner.owner_agent_id is None:
        raise RuntimeError("new owner generation is missing launch fields")
    generation = owner.generation
    expected_suffix = owner.expected_suffix
    owner_agent_id = owner.owner_agent_id
    sid: int | None = None
    try:
        relay_stub = None if plugin_dir is None else _generation_relay_stub(owner.state_dir)
        _pretrust(workspace)
        sid = ava.shell.sessions.new(name=expected_suffix, ttl=ttl_seconds)
        full_name = coding_session_owner.full_session_name(owner_agent_id, sid, expected_suffix)
        active = coding_session_owner.publish_active(
            key,
            generation,
            session_id=sid,
            session_name=full_name,
        )
        with tempfile.TemporaryDirectory(prefix="ava-claude-launch-") as marker_dir:
            marker = Path(marker_dir) / "missing-claude"
            ava.shell.sessions.send(
                sid,
                _claude_command(
                    workspace,
                    caller_instance,
                    failure_marker=marker,
                    relay_stub=relay_stub,
                    relay_plugin_dir=plugin_dir,
                    claude_session=claude_session,
                    resume=resume,
                ),
            )
            _wait_for_ready(sid, failure_marker=marker, resumed=resume)
        guide = impersonator_guide(skill_dir)
        message = _takeover_bootstrap_message(
            owner_agent_id,
            takeover_name,
            takeover_brief,
            guide,
            relay_resident=plugin_dir is not None,
        )
        baseline = _bootstrap_count(claude_session)
        _send_bootstrap(sid, message)
        _verify_start_receipt(
            sid,
            lambda: _takeover_bootstrap_message(
                owner_agent_id,
                takeover_name,
                takeover_brief,
                guide,
                relay_resident=plugin_dir is not None,
            ),
            lambda: _bootstrap_count(claude_session) > baseline,
        )
    except BaseException:
        # Another launch's sweep may have reclaimed this generation's record by
        # now, so its CAS cannot reclaim this PTY. The launcher still owns the
        # numeric id and must reclaim it directly before rolling back its record.
        if sid is not None:
            kill_session_after_failed_launch(sid)
        terminate_generation_after_failed_launch(key, generation)
        raise

    print(f"ready. name={active.expected_suffix} workspace={workspace}")
    _print_owner(active)
    print(f"claude_session={claude_session}")
    return 0


def launch(
    workspace: Path,
    tasks_file: Path | None,
    work_file: Path | None,
    ttl_seconds: float,
    caller_instance: str | None = None,
    impersonation_name: str | None = None,
    brief: str | None = None,
    *,
    skill_dir: Path,
    resume: str | None = None,
    relay_resident: bool = True,
    relay_plugin_dir: Path | None = None,
) -> int:
    """Launch a supervised worker, or a takeover when ``impersonation_name`` is set.

    ``skill_dir`` is the calling skill's own directory: its ``references/``
    holds the collaboration contract and locates the impersonator guide, and
    its ``scripts/`` holds the resident relay plugin. The Claude session id is
    chosen here and printed as ``claude_session``; ``resume`` reopens that
    recorded session instead of starting a new one. Prints one ``key=value``
    per line and returns the exit code.
    """
    from base.agents.messages.external_caller import launch_caller_assignment

    takeover_name: str | None = impersonation_name
    takeover_brief = ""
    if takeover_name is not None:
        if not brief or not brief.strip():
            raise ValueError("a takeover launch needs a non-empty briefing")
        if tasks_file is not None or work_file is not None:
            raise ValueError("a takeover launch reads no task or work file")
        takeover_brief = brief
    elif tasks_file is None or work_file is None:
        raise ValueError("a supervised launch needs its task and work files")

    # Validate before creating files, owner records, or sessions.
    launch_caller_assignment("claude_code", caller_instance)
    claude_session = resume or str(uuid.uuid4())
    _preset_claude_first_run()
    if takeover_name is None:
        assert tasks_file is not None and work_file is not None  # noqa: S101 — checked above
        return _run_supervised_launch(
            workspace,
            tasks_file,
            work_file,
            ttl_seconds,
            caller_instance,
            skill_dir / "references" / "collaboration_protocol.md",
            claude_session,
            resume=resume is not None,
        )
    return _run_takeover_launch(
        workspace,
        takeover_name,
        takeover_brief,
        ttl_seconds,
        caller_instance,
        skill_dir,
        claude_session,
        resume=resume is not None,
        relay_resident=relay_resident,
        relay_plugin_dir=relay_plugin_dir,
    )

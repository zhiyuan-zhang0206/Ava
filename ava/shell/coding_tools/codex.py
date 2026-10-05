"""Launch, inspect, or stop Codex sessions in persistent shells.

Every launch owns a generation of its own under ``(cluster, workspace,
codex)``; several may share a workspace, and a launch first reclaims its dead
siblings. Codex runs on the user's own ``~/.codex`` with per-session ``-c``
overrides, so every session stays resumable by the id the launch prints. A
supervised worker also starts an automatic lifecycle supervisor (the skill's
``watch_work.py``); a takeover (``impersonation_name``) runs file- and
supervisor-less with its briefing inlined in the launch message. A takeover
also wires the explicit shared app-server
topology: a private ``codex app-server --listen`` socket, the TUI connected to
it with ``--remote``, and the endpoint carried into the launch message so the
request records it (``--codex-remote``) and the relay delivers into the same
server.

The ``ava-guide.external-agents`` skill's ``spawn_codex.py`` is the command-line
entry; it passes its own skill directory, whose ``references/`` holds the
collaboration contract (and locates the impersonator guide) and whose
``scripts/`` holds the supervisor script.
"""

from __future__ import annotations

import contextlib
import json
import re
import shlex
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import ava
from base.sessions import coding_session_owner

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

DEFAULT_TTL_SECONDS = 4 * 3600
_SUPERVISOR_TTL_PADDING_SECONDS = 300


_SESSION_ID = re.compile(
    r"Session:\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)


def _config_overrides(workspace: Path) -> str:
    """Per-session ``-c`` overrides on top of the user's own ``~/.codex``.

    Codex runs on the default home exactly as a person's own terminal would, so
    its sessions stay resumable with ``codex resume``. The overrides touch no
    file: the workspace is trusted for this session only (the table replaces
    the ``projects`` map for this process), and the startup update check is
    off, since an unattended launch must never answer an "update now" prompt.
    """
    trusted = f'projects={{{json.dumps(workspace.as_posix())}={{trust_level="trusted"}}}}'
    return f"-c {shlex.quote(trusted)} -c check_for_update_on_startup=false"


def _wait_for_ready(sid: int, timeout: float = 90.0) -> None:
    """Wait until Codex has finished MCP startup after publishing its handle.

    len(output) > 50 alone is a FALSE ready signal: the TUI renders its frame
    during "Starting MCP servers (0/2)", and a message sent then has its Enter
    swallowed — the text parks in the composer and the session looks alive but
    never works (empirical 2026-08-26 #3438, 2026-09-02 #5655, 2026-09-03
    #5779). Treat "Starting MCP" as not-ready until it disappears.
    """
    print(f"waiting for session {sid} to be ready (MCP startup + render)...")
    deadline = time.time() + timeout
    rendered = False
    while time.time() < deadline:
        output = ava.shell.sessions.capture(sid, scrollback=False)
        if len(output) > 50:
            rendered = True
            if "Starting MCP" in output:
                time.sleep(2)
                continue
            time.sleep(2)
            print("  -> ready (MCP startup finished)")
            return
        time.sleep(1)
    print(
        f"  -> timeout after {timeout:.0f} s "
        f"({'rendered, still starting MCP' if rendered else 'never rendered'}), sending anyway"
    )


def _dead_session_warning(action: str, exc: ValueError) -> None:
    """Warn — never raise — when a receipt checkpoint meets a dead session.

    The checkpoint's contract is loud-not-fatal: the operator sees the warning
    and checks the session. An escaped ValueError is read as launch failure by
    the caller, which kills the session and rolls the generation back.
    """
    print(
        "  -> WARNING: submission unverified "
        f"({action} failed: {exc}); the session may have ended. "
        "Check the session before relying on it."
    )


def _verify_submitted(sid: int, timeout: float = 60.0) -> None:
    """The bootstrap message must actually submit, not park in the composer.

    Submission signal = capture shows "Working" (Codex's busy state line). If
    it does not appear within ``timeout``, send one Enter (Enter submits a single-line queued message; the historical
    "Tab submits" note is wrong — 2026-09-03 #5779 re-test) and re-check.
    Kept loud but not fatal: a dead session's capture()/send_keys() refusal
    warns and returns — an escaped ValueError would roll the launch back (the
    caller kills the session and terminates the generation on exceptions).
    """
    print("verifying the bootstrap message was submitted...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            output = ava.shell.sessions.capture(sid, scrollback=False)
        except ValueError as exc:
            _dead_session_warning("capture", exc)
            return
        if "Working" in output:
            print("  -> submitted (Working visible)")
            return
        time.sleep(3)
    print("  -> not submitted within window; sending Enter once")
    try:
        ava.shell.sessions.send_keys(sid, "Enter")
    except ValueError as exc:
        _dead_session_warning("Enter retry", exc)
        return
    time.sleep(5)
    try:
        output = ava.shell.sessions.capture(sid, scrollback=False)
    except ValueError as exc:
        _dead_session_warning("capture", exc)
        return
    if "Working" in output:
        print("  -> submitted after Enter retry")
    else:
        print(
            "  -> WARNING: still no Working signal. The contract message may be "
            "parked in the composer ('tab to queue message'). Check the session "
            "and press Enter manually; see codex-tui-first-message trap in memory."
        )


def _read_session_id(sid: int, timeout: float = 20.0) -> str:
    """The Codex session id, read from the ``/status`` card of the ready TUI.

    It is the id ``codex resume <id>`` takes, and it is what a person sees in
    the same card. Sent before the first message, so the composer is empty; a
    ``/status`` that parked gets one Enter. No id means the launch cannot be
    resumed later, so it fails.
    """
    ava.shell.sessions.send(sid, "/status")
    for attempt in range(2):
        deadline = time.time() + timeout / 2
        while time.time() < deadline:
            found = _SESSION_ID.search(ava.shell.sessions.capture(sid, lines=80))
            if found is not None:
                print(f"  -> codex session {found.group(1)}")
                return found.group(1)
            time.sleep(1)
        if attempt == 0:
            ava.shell.sessions.send_keys(sid, "Enter")
    raise RuntimeError(
        f"could not read the Codex session id from /status in session {sid}; "
        "the launch is rolled back because it could not be resumed later"
    )


def _supervisor_code(owner: coding_session_owner.CodingSessionOwner, watcher: Path) -> str:
    """Run the skill's supervisor ``watch`` for exactly this generation."""
    if owner.generation is None or owner.owner_agent_id is None or owner.work_file is None:
        raise RuntimeError("launching owner is missing supervisor inputs")
    return (
        "import runpy\n"
        f"_watch = runpy.run_path({str(watcher)!r})['watch']\n"
        f"_watch({str(owner.work_file)!r}, cluster={owner.key.cluster!r}, "
        f"workspace={owner.key.workspace!r}, generation={owner.generation!r}, "
        f"owner_agent_id={owner.owner_agent_id!r})\n"
    )


def _supervisor_command(owner: coding_session_owner.CodingSessionOwner, watcher: Path) -> str:
    """The supervisor PTY's command, with the owner's identity pinned in its environment."""
    code = _supervisor_code(owner, watcher)
    return (
        f"exec env AVA_AGENT_ID={owner.owner_agent_id} "
        f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    )


def _supervisor_name(owner: coding_session_owner.CodingSessionOwner) -> str:
    if owner.generation is None:
        raise RuntimeError("launching owner has no generation")
    return coding_session_owner.supervisor_suffix(owner.key, owner.generation)


def _launch_supervisor(
    owner: coding_session_owner.CodingSessionOwner, ttl_seconds: float, watcher: Path
) -> tuple[int, str]:
    """Launch a quiet PTY supervisor that cannot resurrect a terminated owner."""
    supervisor_ttl = min(ttl_seconds + _SUPERVISOR_TTL_PADDING_SECONDS, 86_400)
    suffix = _supervisor_name(owner)
    session_id = ava.shell.sessions.new(
        name=suffix,
        ttl=supervisor_ttl,
    )
    command = _supervisor_command(owner, watcher)
    try:
        ava.shell.sessions.send(session_id, command)
    except BaseException:
        kill_session_after_failed_launch(session_id)
        raise
    if owner.owner_agent_id is None:
        raise RuntimeError("launching owner has no agent identity")
    return session_id, coding_session_owner.full_session_name(
        owner.owner_agent_id, session_id, suffix
    )


def _app_server_endpoint(key: coding_session_owner.CodingSessionKey, generation: str) -> str:
    """The single endpoint both the TUI and the codex relay connect to.

    The socket lives in a short per-user directory that
    ``codex_app_server_socket`` creates and verifies (the mkdir below is then a
    no-op); the app server binds the socket itself.
    """
    socket_path = coding_session_owner.codex_app_server_socket(key, generation)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    return f"unix://{socket_path}"


def _app_server_command(
    owner: coding_session_owner.CodingSessionOwner,
    workspace: Path,
    endpoint: str,
    caller_instance: str | None = None,
) -> str:
    """Start the shared app server plus the janitor that outlives the kill rounds.

    The kill path signals the shell's and the foreground process group only, so
    a background child in its own group survives it; the janitor is that child's
    cleanup owner — it watches the session child (this shell, then the exec'd
    TUI) and ends the server when it dies, then removes the socket. The
    hands-off policy is configured on the server itself: the remote TUI's flags
    do not reach the server's tools.

    The janitor's cadence bounds are fixed launch constants, not tuning knobs:
    a 2s dead-check interval reaps within a few seconds of session death at one
    cheap sleep, and a 1s grace lets the server exit on SIGTERM (and unlink its
    socket) before the SIGKILL.
    """
    from base.agents.messages.external_caller import launch_caller_assignment

    if owner.state_dir is None:
        raise RuntimeError("launching owner has no generation state directory")
    log_path = _app_server_log_path(owner)
    socket_path = endpoint.removeprefix("unix://")
    janitor = (
        "{ while kill -0 $$ 2>/dev/null && kill -0 $AP 2>/dev/null; do sleep 2; done; "
        "if ! kill -0 $$ 2>/dev/null; then "
        "if ps -p $AP -o command= 2>/dev/null | grep -q 'app-server'; then "
        "kill $AP 2>/dev/null; sleep 1; kill -9 $AP 2>/dev/null; fi; fi; "
        f"rm -f {shlex.quote(socket_path)}; }}"
        " &"
    )
    # The server pid travels as ${!}: interactive panes run with history
    # expansion, where a bare $! can abort the whole line (review C1); ${!} is
    # accepted by bash, dash and zsh alike, so the swap carries no behavior
    # risk (review N8).
    return (
        f"(cd {shlex.quote(workspace.as_posix())} && "
        f"{launch_caller_assignment('codex', caller_instance)}"
        f"exec codex app-server --listen {shlex.quote(endpoint)}"
        ' -c approval_policy="never" -c sandbox_mode="danger-full-access"'
        f" {_config_overrides(workspace)}"
        f" > {shlex.quote(str(log_path))} 2>&1) & AP=${{!}}; "
        f"{janitor}"
    )


def _app_server_log_path(owner: coding_session_owner.CodingSessionOwner) -> Path:
    """The shared app server's stdout/stderr log inside the owner's state dir."""
    assert owner.state_dir is not None  # noqa: S101 — callers run after the launch-field check
    return owner.state_dir / "app-server.log"


def _wait_for_app_server(
    endpoint: str, timeout: float = 20.0, *, log_path: Path | None = None
) -> None:
    """Block until the shared app server accepts a connection on its socket.

    Socket acceptance is the readiness fact (the app server writes no startup
    line); a launch whose endpoint never answers fails loudly instead of
    starting a TUI that queues nothing. ``log_path``, when given, names the
    server's own log in the timeout error so a failing launch points at the
    evidence (review N3).

    The 20s timeout and 0.5s probe cadence are fixed launch bounds: app-server
    startup measured ~1-2s on codex 0.153.4, so 20s bounds one wait and an
    absent or older app server fails the launch loudly instead of hanging it.
    """
    path = endpoint.removeprefix("unix://")
    print(f"waiting for the shared codex app server at {endpoint}...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if Path(path).exists():
            with contextlib.suppress(OSError):
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                # Local connect-probe bound: the house 2s for same-machine
                # sockets, as in the PTY launcher.
                probe.settimeout(2.0)
                try:
                    probe.connect(path)
                finally:
                    probe.close()
                print("  -> app server ready")
                return
        time.sleep(0.5)
    detail = f" (app-server log: {log_path})" if log_path is not None else ""
    raise RuntimeError(
        f"the shared codex app server did not become ready at {endpoint}{detail}; "
        "check that the installed codex supports `app-server --listen` and `--remote` "
        "(see canonical_codex_owner.md) — the takeover launch is refused without it"
    )


def _codex_command(
    workspace: Path,
    caller_instance: str | None = None,
    remote: str | None = None,
    *,
    resume: str | None = None,
) -> str:
    """Build the interactive TUI command; a takeover clears the screen first.

    The shared app server line has already filled the screen with its echo,
    which would otherwise pass for a rendered TUI frame and let the launch
    message park in the composer (the codex-tui-first-message trap). With
    ``resume`` the TUI reopens that recorded session (``codex resume <id>``)
    instead of starting a new one.
    """
    from base.agents.messages.external_caller import launch_caller_assignment

    prefix = "clear && " if remote is not None else ""
    subcommand = f"resume {shlex.quote(resume)} " if resume is not None else ""
    remote_flag = f"--remote {shlex.quote(remote)} " if remote is not None else ""
    return (
        f"{prefix}cd {shlex.quote(workspace.as_posix())} && "
        f"{launch_caller_assignment('codex', caller_instance)}"
        f"exec codex {subcommand}{remote_flag}--dangerously-bypass-approvals-and-sandbox "
        f"{_config_overrides(workspace)}"
    )


def _takeover_bootstrap_message(
    agent_id: int, name: str, brief: str, codex_remote: str, guide: Path
) -> str:
    """Inline the briefing and the shared app-server endpoint; no task/work file."""
    from ava.impersonation.launch import bootstrap_message

    return bootstrap_message(agent_id, name, "codex", brief, guide, codex_remote=codex_remote)


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
    if owner.supervisor_session_id is not None:
        print(f"supervisor_session_id={owner.supervisor_session_id}")
    if owner.supervisor_session_name is not None:
        print(f"supervisor_session_name={owner.supervisor_session_name}")
    if owner.tasks_file is not None:
        print(f"tasks_file={owner.tasks_file}")
    if owner.work_file is not None:
        print(f"work_file={owner.work_file}")


def status(workspace: Path) -> int:
    """Print every generation recorded for the workspace."""
    return _owner_status(
        coding_session_owner.canonical_key(workspace, tool="codex"),
        _print_owner,
    )


def cancel(workspace: Path, generation: str) -> int:
    """Stop and terminalize exactly this generation."""
    return _cancel_generation(
        coding_session_owner.canonical_key(workspace, tool="codex"),
        generation,
        _print_owner,
    )


@dataclass(frozen=True)
class _LaunchRequest:
    """One launch's inputs, checked: a takeover carries a name and briefing and
    no files; a supervised worker carries both files."""

    workspace: Path
    tasks_file: Path | None
    work_file: Path | None
    ttl_seconds: float
    caller_instance: str | None
    takeover_name: str | None
    takeover_brief: str
    skill_dir: Path
    resume: str | None


def _checked_brief(
    takeover_name: str | None, brief: str | None, tasks_file: Path | None, work_file: Path | None
) -> str:
    """The takeover briefing, or "" for a supervised launch; mismatched inputs raise."""
    if takeover_name is not None:
        if not brief or not brief.strip():
            raise ValueError("a takeover launch needs a non-empty briefing")
        if tasks_file is not None or work_file is not None:
            raise ValueError("a takeover launch reads no task or work file")
        return brief
    if tasks_file is None or work_file is None:
        raise ValueError("a supervised launch needs its task and work files")
    return ""


def _start_codex(
    sid: int,
    key: coding_session_owner.CodingSessionKey,
    owner: coding_session_owner.CodingSessionOwner,
    generation: str,
    owner_agent_id: int,
    request: _LaunchRequest,
) -> tuple[str | None, str]:
    """Start Codex in the published PTY and deliver its launch message.

    Returns the shared app-server endpoint of a takeover (else None) and the
    Codex session id, which ``codex resume`` takes after an interruption.
    """
    remote: str | None = None
    if request.takeover_name is not None:
        remote = _app_server_endpoint(key, generation)
        ava.shell.sessions.send(
            sid, _app_server_command(owner, request.workspace, remote, request.caller_instance)
        )
        _wait_for_app_server(remote, log_path=_app_server_log_path(owner))
    ava.shell.sessions.send(
        sid,
        _codex_command(
            request.workspace, request.caller_instance, remote=remote, resume=request.resume
        ),
    )
    _wait_for_ready(sid)
    codex_session = _read_session_id(sid)
    if request.resume is not None and codex_session != request.resume:
        raise RuntimeError(
            f"codex reopened session {codex_session}, not the requested {request.resume}"
        )
    if request.takeover_name is not None:
        assert remote is not None  # noqa: S101 — set above for takeovers
        message = _takeover_bootstrap_message(
            owner_agent_id,
            request.takeover_name,
            request.takeover_brief,
            remote,
            impersonator_guide(),
        )
    else:
        assert request.tasks_file is not None and request.work_file is not None  # noqa: S101
        message = worker_bootstrap(
            request.skill_dir / "references" / "collaboration_protocol.md",
            request.workspace,
            request.tasks_file,
            request.work_file,
            resumed=request.resume is not None,
        )
    ava.shell.sessions.send(sid, message)
    _verify_submitted(sid)
    return remote, codex_session


def _start_generation(
    key: coding_session_owner.CodingSessionKey,
    owner: coding_session_owner.CodingSessionOwner,
    request: _LaunchRequest,
) -> tuple[coding_session_owner.CodingSessionOwner, str | None, str]:
    """Supervise, publish and start one fresh generation; roll back on failure.

    A takeover's generation state directory holds its app-server log.
    """
    if (
        owner.generation is None
        or owner.expected_suffix is None
        or owner.state_dir is None
        or owner.owner_agent_id is None
    ):
        raise RuntimeError("new owner generation is missing launch fields")
    generation = owner.generation
    expected_suffix = owner.expected_suffix
    owner_agent_id = owner.owner_agent_id
    sid: int | None = None
    try:
        if request.takeover_name is not None:
            owner.state_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        else:
            watcher_id, watcher_name = _launch_supervisor(
                owner, request.ttl_seconds, request.skill_dir / "scripts" / "watch_work.py"
            )
            owner = coding_session_owner.attach_supervisor(
                key,
                generation,
                session_id=watcher_id,
                session_name=watcher_name,
            )
        sid = ava.shell.sessions.new(name=expected_suffix, ttl=request.ttl_seconds)
        full_name = coding_session_owner.full_session_name(owner_agent_id, sid, expected_suffix)
        active = coding_session_owner.publish_active(
            key,
            generation,
            session_id=sid,
            session_name=full_name,
        )
        remote, codex_session = _start_codex(sid, key, owner, generation, owner_agent_id, request)
    except BaseException:
        # Another launch's sweep may have reclaimed this generation's record by
        # now, so its CAS cannot reclaim this PTY. The launcher still owns the
        # numeric id and must reclaim it directly before rolling back its record.
        if sid is not None:
            kill_session_after_failed_launch(sid)
        terminate_generation_after_failed_launch(key, generation)
        raise
    return active, remote, codex_session


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
) -> int:
    """Launch a supervised worker, or a takeover when ``impersonation_name`` is set.

    ``skill_dir`` is the calling skill's own directory: its ``references/``
    holds the collaboration contract and locates the impersonator guide, and
    its ``scripts/`` holds the supervisor script. ``resume`` reopens a
    recorded Codex session (the ``codex_session`` an earlier launch printed)
    instead of starting a new one. Prints one ``key=value`` per line and
    returns the exit code.
    """
    from base.agents.messages.external_caller import launch_caller_assignment

    request = _LaunchRequest(
        workspace,
        tasks_file,
        work_file,
        ttl_seconds,
        caller_instance,
        impersonation_name,
        _checked_brief(impersonation_name, brief, tasks_file, work_file),
        skill_dir,
        resume,
    )
    # Validate before creating files, owner records, or sessions.
    launch_caller_assignment("codex", caller_instance)
    if tasks_file is not None and work_file is not None:
        init_file(tasks_file, "")
        init_file(work_file, "STATUS: WORKING\n\n## Log\n\n")
    key = coding_session_owner.canonical_key(workspace, tool="codex")
    owner = new_generation(key, tasks_file=tasks_file, work_file=work_file, ttl_seconds=ttl_seconds)
    active, remote, codex_session = _start_generation(key, owner, request)
    print(f"ready. name={active.expected_suffix} workspace={workspace}")
    if remote is not None:
        print(f"codex_app_server={remote}")
    _print_owner(active)
    print(f"codex_session={codex_session}")
    return 0

#!/usr/bin/env python3
"""Create, adopt, inspect, or stop the canonical Codex workspace generation.

The active identity is ``(cluster, canonical workspace, codex)``. A launch
publishes one generation-owned record and gives that generation a private
``CODEX_HOME``. A supervised worker also starts an automatic lifecycle
supervisor; a takeover (``--impersonate-self``) runs file- and supervisor-less
with its briefing inlined in the launch message. A concurrent or cross-agent
caller adopts the live record instead of stacking another Codex process.
A takeover also wires the explicit shared app-server topology: a private
``codex app-server --listen`` socket, the TUI connected to it with ``--remote``,
and the endpoint carried into the launch message so the request records it
(``--codex-remote``) and the relay queues into the same server.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shlex
import shutil
import socket
import sys
import time
from pathlib import Path

import ava
from shared import coding_session_owner
from shared.agents import AgentNotFound, AgentStatus

_DEFAULT_TTL_SECONDS = 4 * 3600
_SUPERVISOR_TTL_PADDING_SECONDS = 300


def _resolve_dir(dir_path: str) -> Path:
    path = Path(dir_path).expanduser().resolve()
    if not path.is_dir():
        print(f"error: {path} is not a directory or does not exist", file=sys.stderr)
        raise SystemExit(1)
    return path


def _resolve_file(workspace: Path, raw: str) -> Path:
    """Absolute path stays as given; a relative one is taken against the workspace."""
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (workspace / path).resolve()


def _init_file(path: Path, initial: str) -> None:
    """Create the file with ``initial`` if absent, including parent directories."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(initial, encoding="utf-8")


def _project_header(workspace: Path) -> str:
    return f"[projects.{json.dumps(workspace.as_posix())}]"


def _seed_codex_home(
    codex_home: Path,
    workspace: Path,
    *,
    source_home: Path | None = None,
) -> None:
    """Seed only immutable launch inputs, never SQLite, sessions, or logs."""
    source = source_home or (Path.home() / ".codex")
    codex_home.mkdir(parents=True, exist_ok=False)
    codex_home.chmod(0o700)
    # Symlink auth.json rather than snapshot it: tokens the user adds after
    # spawn (e.g. a fresh MCP OAuth login) must be visible to this session
    # immediately. A spawn-time copy silently freezes them (empirical 2026-09-04:
    # real auth.json updated at 00:13, private-home copy still the 23:34 snapshot).
    # config.toml stays a copy below because it is rewritten per project.
    auth_source = source / "auth.json"
    if auth_source.is_file():
        auth_target = codex_home / "auth.json"
        try:
            auth_target.symlink_to(auth_source)
        except OSError:
            shutil.copyfile(auth_source, auth_target)
        auth_target.chmod(0o600)

    config_source = source / "config.toml"
    config = config_source.read_text(encoding="utf-8") if config_source.is_file() else ""
    section = _project_header(workspace)
    if section not in config:
        config = config.rstrip("\n") + f'\n\n{section}\ntrust_level = "trusted"\n'
    config_target = codex_home / "config.toml"
    config_target.write_text(config.lstrip("\n"), encoding="utf-8")
    config_target.chmod(0o600)


def _contract_path() -> Path:
    return Path(__file__).resolve().parent / "collaboration_protocol.md"


def _watcher_path() -> Path:
    return Path(__file__).resolve().parent / "watch_work.py"


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


def _verify_submitted(sid: int, codex_home: Path, timeout: float = 60.0) -> None:
    """The bootstrap message must actually submit, not park in the composer.

    Submission signal = capture shows "Working" (Codex's busy state line) OR a
    new sessions jsonl appears under codex_home. If neither within ``timeout``,
    send one Enter (Enter submits a single-line queued message; the historical
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
        sessions_dir = codex_home / "sessions"
        if sessions_dir.is_dir():
            now = time.time()
            recent = any(
                p.is_file() and now - p.stat().st_mtime < 30 for p in sessions_dir.rglob("*.jsonl")
            )
            if recent:
                print("  -> submitted (fresh session jsonl)")
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


def _owner_terminated(agent_id: int) -> bool:
    try:
        return ava.agents.get_status(agent_id) is AgentStatus.TERMINATED
    except AgentNotFound:
        return True
    except Exception:
        # An unavailable gateway cannot prove an owner dead. The supervisor and
        # expiry retain responsibility; guessing here could kill active work.
        return False


def _supervisor_code(owner: coding_session_owner.CodingSessionOwner) -> str:
    if owner.generation is None or owner.owner_agent_id is None or owner.work_file is None:
        raise RuntimeError("launching owner is missing supervisor inputs")
    return (
        "import os, runpy\n"
        f"os.environ['AVA_AGENT_ID'] = {str(owner.owner_agent_id)!r}\n"
        f"_watch = runpy.run_path({str(_watcher_path())!r})['watch']\n"
        f"_watch({str(owner.work_file)!r}, cluster={owner.key.cluster!r}, "
        f"workspace={owner.key.workspace!r}, generation={owner.generation!r}, "
        f"owner_agent_id={owner.owner_agent_id!r})\n"
    )


def _supervisor_name(owner: coding_session_owner.CodingSessionOwner) -> str:
    if owner.generation is None:
        raise RuntimeError("launching owner has no generation")
    return coding_session_owner.supervisor_suffix(owner.key, owner.generation)


def _launch_supervisor(
    owner: coding_session_owner.CodingSessionOwner, ttl_seconds: float
) -> tuple[int, str]:
    """Launch a quiet PTY supervisor that cannot resurrect a terminated owner."""
    supervisor_ttl = min(ttl_seconds + _SUPERVISOR_TTL_PADDING_SECONDS, 86_400)
    suffix = _supervisor_name(owner)
    session_id = ava.shell.sessions.new(
        name=suffix,
        ttl=supervisor_ttl,
    )
    command = f"exec {shlex.quote(sys.executable)} -c {shlex.quote(_supervisor_code(owner))}"
    try:
        ava.shell.sessions.send(session_id, command)
    except BaseException:
        with contextlib.suppress(Exception):
            ava.shell.sessions.kill(session_id)
        raise
    if owner.owner_agent_id is None:
        raise RuntimeError("launching owner has no agent identity")
    return session_id, coding_session_owner.full_session_name(
        owner.owner_agent_id, session_id, suffix
    )


def _app_server_endpoint(key: coding_session_owner.CodingSessionKey, generation: str) -> str:
    """The single endpoint both the TUI and the codex relay connect to.

    Prepares the socket's private run directory; the app server binds the
    socket itself.
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

    The janitor's cadence bounds are reference-script defaults, not config —
    the script is copied and run standalone, so a knob travels in the file
    (task #3696 exception inventory): a 2s dead-check interval reaps within a
    few seconds of session death at one cheap sleep, and a 1s grace lets the
    server exit on SIGTERM (and unlink its socket) before the SIGKILL.
    """
    from shared.external_caller import launch_caller_assignment

    if owner.state_dir is None:
        raise RuntimeError("launching owner has no isolated state directory")
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
        f"CODEX_HOME={shlex.quote(str(owner.state_dir))} "
        f"{launch_caller_assignment('codex', caller_instance)}"
        f"exec codex app-server --listen {shlex.quote(endpoint)}"
        ' -c approval_policy="never" -c sandbox_mode="danger-full-access"'
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

    The 20s timeout and 0.5s probe cadence are reference-script defaults, not
    config — the script is copied and run standalone (task #3696 exception
    inventory): app-server startup measured ~1-2s on codex 0.153.4, so 20s
    bounds one wait and an absent or older app server fails the launch loudly
    instead of hanging it.
    """
    path = endpoint.removeprefix("unix://")
    print(f"waiting for the shared codex app server at {endpoint}...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if Path(path).exists():
            with contextlib.suppress(OSError):
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                # Local connect-probe bound; the house 2s for same-machine
                # sockets (shared/pty_sessions/launch.py). Reference-script
                # default, not config (task #3696 exception inventory).
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
    owner: coding_session_owner.CodingSessionOwner,
    workspace: Path,
    caller_instance: str | None = None,
    remote: str | None = None,
) -> str:
    """Build the interactive TUI command; a takeover clears the screen first.

    The shared app server line has already filled the screen with its echo,
    which would otherwise pass for a rendered TUI frame and let the launch
    message park in the composer (the codex-tui-first-message trap).
    """
    from shared.external_caller import launch_caller_assignment

    if owner.state_dir is None:
        raise RuntimeError("launching owner has no isolated state directory")
    prefix = "clear && " if remote is not None else ""
    remote_flag = f"--remote {shlex.quote(remote)} " if remote is not None else ""
    return (
        f"{prefix}cd {shlex.quote(workspace.as_posix())} && "
        f"CODEX_HOME={shlex.quote(str(owner.state_dir))} "
        f"{launch_caller_assignment('codex', caller_instance)}"
        f"exec codex {remote_flag}--dangerously-bypass-approvals-and-sandbox"
    )


def _bootstrap_message(
    workspace: Path,
    tasks_file: Path,
    work_file: Path,
) -> str:
    """Build the complete durable-state handoff for a fresh Codex process."""
    return (
        f"Read the collaboration contract at {_contract_path()} and follow it. "
        f"Your workspace is {workspace}. "
        f"Your task file (read-only for you) is {tasks_file}. "
        f"Your work file (yours to write, STATUS + log) is {work_file}. "
        "Now read the task file and start working."
    )


def _takeover_bootstrap_message(agent_id: int, name: str, brief: str, codex_remote: str) -> str:
    """Inline the briefing and the shared app-server endpoint; no task/work file."""
    from ava._impersonation_launch import bootstrap_message

    guide = (
        Path(__file__).resolve().parents[4]
        / ".agents"
        / "skills"
        / "impersonator-guide"
        / "SKILL.md"
    )
    return bootstrap_message(agent_id, name, "codex", brief, guide, codex_remote=codex_remote)


def _print_owner(owner: coding_session_owner.CodingSessionOwner, *, adopted: bool) -> None:
    print(f"adopted={'true' if adopted else 'false'}")
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
    if owner.state_dir is not None:
        print(f"codex_home={owner.state_dir}")
    if owner.tasks_file is not None:
        print(f"tasks_file={owner.tasks_file}")
    if owner.work_file is not None:
        print(f"work_file={owner.work_file}")


def _status(key: coding_session_owner.CodingSessionKey) -> int:
    owner = coding_session_owner.read(key)
    _print_owner(owner, adopted=False)
    if owner.error:
        print(f"error={owner.error}", file=sys.stderr)
    return 1 if owner.status == "invalid" else 0


def _cancel(key: coding_session_owner.CodingSessionKey, generation: str) -> int:
    stopped = coding_session_owner.terminate_generation(
        key,
        generation,
        reason="explicit-cancel",
    )
    if not stopped:
        print("cancel refused: generation is not the current canonical owner", file=sys.stderr)
        return 1
    _print_owner(coding_session_owner.read(key), adopted=False)
    return 0


def _claim_canonical(
    key: coding_session_owner.CodingSessionKey,
    *,
    tasks_file: Path | None,
    work_file: Path | None,
    ttl_seconds: float,
) -> coding_session_owner.CodingSessionClaim:
    """Create or adopt, waiting through another claimant's bounded launch."""
    previous = coding_session_owner.read(key)
    terminated_generation = None
    if (
        previous.generation is not None
        and previous.owner_agent_id is not None
        and _owner_terminated(previous.owner_agent_id)
    ):
        terminated_generation = previous.generation
    while True:
        result = coding_session_owner.claim(
            key,
            owner_agent_id=ava.self.AGENT_ID,
            tasks_file=tasks_file,
            work_file=work_file,
            ttl_seconds=ttl_seconds,
            terminated_generation=terminated_generation,
        )
        if result.action != "busy":
            return result
        time.sleep(0.25)


def _launch(
    workspace: Path,
    tasks_file: Path | None,
    work_file: Path | None,
    ttl_seconds: float,
    caller_instance: str | None = None,
    impersonation_name: str | None = None,
    brief: str | None = None,
) -> int:
    from shared.external_caller import launch_caller_assignment

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
    launch_caller_assignment("codex", caller_instance)
    if takeover_name is None:
        assert tasks_file is not None and work_file is not None  # noqa: S101 — checked above
        _init_file(tasks_file, "")
        _init_file(work_file, "STATUS: WORKING\n\n## Log\n\n")
    key = coding_session_owner.canonical_key(workspace, tool="codex")
    claim = _claim_canonical(
        key,
        tasks_file=tasks_file,
        work_file=work_file,
        ttl_seconds=ttl_seconds,
    )
    if claim.action == "adopt":
        if takeover_name is not None:
            raise RuntimeError(
                "a takeover needs a fresh coding workspace; this workspace already has a live "
                "generation - cancel it with --cancel-generation first"
            )
        _print_owner(claim.owner, adopted=True)
        return 0
    owner = claim.owner
    if (
        owner.generation is None
        or owner.expected_suffix is None
        or owner.state_dir is None
        or owner.owner_agent_id is None
    ):
        raise RuntimeError("new canonical owner is missing launch fields")
    generation = owner.generation
    expected_suffix = owner.expected_suffix
    owner_agent_id = owner.owner_agent_id
    sid: int | None = None
    remote: str | None = None
    try:
        _seed_codex_home(owner.state_dir, workspace)
        if takeover_name is None:
            watcher_id, watcher_name = _launch_supervisor(owner, ttl_seconds)
            owner = coding_session_owner.attach_supervisor(
                key,
                generation,
                session_id=watcher_id,
                session_name=watcher_name,
            )
        sid = ava.shell.sessions.new(name=expected_suffix, ttl=ttl_seconds)
        full_name = coding_session_owner.full_session_name(owner_agent_id, sid, expected_suffix)
        active = coding_session_owner.publish_active(
            key,
            generation,
            session_id=sid,
            session_name=full_name,
        )
        if takeover_name is not None:
            remote = _app_server_endpoint(key, generation)
            ava.shell.sessions.send(
                sid, _app_server_command(owner, workspace, remote, caller_instance)
            )
            _wait_for_app_server(remote, log_path=_app_server_log_path(owner))
        ava.shell.sessions.send(
            sid, _codex_command(owner, workspace, caller_instance, remote=remote)
        )
        _wait_for_ready(sid)
        if takeover_name is not None:
            assert remote is not None  # noqa: S101 — set above for takeovers
            message = _takeover_bootstrap_message(
                owner_agent_id, takeover_name, takeover_brief, remote
            )
        else:
            assert tasks_file is not None and work_file is not None  # noqa: S101 — checked above
            message = _bootstrap_message(workspace, tasks_file, work_file)
        ava.shell.sessions.send(sid, message)
        _verify_submitted(sid, owner.state_dir)
    except BaseException:
        # A replacement may own the canonical record by now, so its generation
        # CAS cannot reclaim this PTY. The old launcher still owns the numeric id
        # and must reclaim it directly before rolling back its record.
        if sid is not None:
            with contextlib.suppress(Exception):
                ava.shell.sessions.kill(sid)
        with contextlib.suppress(Exception):
            coding_session_owner.terminate_generation(
                key,
                generation,
                reason="launch-failed",
            )
        raise

    print(f"ready. name={active.expected_suffix} workspace={workspace}")
    if remote is not None:
        print(f"codex_app_server={remote}")
    _print_owner(active, adopted=False)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Launch or adopt the canonical supervised Codex workspace generation."
    )
    parser.add_argument("workspace", help="Canonical workspace directory for Codex.")
    parser.add_argument(
        "--caller-instance",
        default=None,
        help="opt in to v1 external provenance (bounded instance ID); requires target protocol support",
    )
    parser.add_argument(
        "--tasks-file",
        default=None,
        help="Task input file, absolute or relative to the workspace "
        "(default: tasks.md). Supervised worker mode only.",
    )
    parser.add_argument(
        "--work-file",
        default=None,
        help="STATUS and log file, absolute or relative to the workspace "
        "(default: work.md). Supervised worker mode only.",
    )
    parser.add_argument(
        "--ttl-seconds",
        type=float,
        default=_DEFAULT_TTL_SECONDS,
        help="Task-adapted hard expiry, up to one day (default: %(default)s).",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--status", action="store_true", help="Print the canonical owner record.")
    action.add_argument(
        "--cancel-generation",
        metavar="GENERATION",
        help="Stop and terminalize exactly this canonical generation.",
    )
    parser.add_argument(
        "--impersonate-self", action="store_true", help="replace the launching Ava agent"
    )
    parser.add_argument("--impersonation-name", help="display name for the takeover session")
    parser.add_argument(
        "--brief",
        default=None,
        help="Takeover briefing text, inlined verbatim into the launch message. "
        "Required with --impersonate-self; a takeover reads no files.",
    )
    args = parser.parse_args()
    if args.impersonate_self:
        from ava._boot import require_agent_id

        require_agent_id()
        if args.status or args.cancel_generation:
            parser.error("--impersonate-self requires a new launch")
        if args.tasks_file is not None or args.work_file is not None:
            parser.error(
                "--tasks-file/--work-file serve the supervised worker mode; a takeover reads no files"
            )
        if args.brief is None or not args.brief.strip():
            parser.error("--impersonate-self requires a non-empty --brief")
    else:
        if args.impersonation_name is not None:
            parser.error("--impersonation-name requires --impersonate-self")
        if args.brief is not None:
            parser.error("--brief requires --impersonate-self")

    workspace = Path(args.workspace).expanduser().resolve()
    if not args.status and not args.cancel_generation:
        workspace = _resolve_dir(args.workspace)
    key = coding_session_owner.canonical_key(workspace, tool="codex")
    if args.status:
        return _status(key)
    if args.cancel_generation:
        return _cancel(key, args.cancel_generation)
    tasks_file = None
    work_file = None
    if not args.impersonate_self:
        tasks_file = _resolve_file(workspace, args.tasks_file or "tasks.md")
        work_file = _resolve_file(workspace, args.work_file or "work.md")
    return _launch(
        workspace,
        tasks_file,
        work_file,
        args.ttl_seconds,
        args.caller_instance,
        (args.impersonation_name or workspace.name) if args.impersonate_self else None,
        args.brief,
    )


if __name__ == "__main__":
    sys.exit(main())

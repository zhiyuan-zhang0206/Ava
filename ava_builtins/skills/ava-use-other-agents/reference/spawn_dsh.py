#!/usr/bin/env python3
"""Launch DeepSeek Harness (dsh) to take over the launching Ava agent, or inspect/stop it.

dsh runs only as a takeover (``--impersonate-self``): file- and supervisor-less,
with the briefing inlined in the launch message. The launcher boots dsh's
shipped ``headless`` profile with the Ava relay plugin patched in
(``ava-relay-dsh/ava-relay.mjs``). The plugin stands in for the one-shot
headless runner: it opens one persistent session, submits the launch message,
echoes the session to the PTY, and starts the Ava relay from the credential
stub ``ava impersonate request --provider dsh`` writes. The generation-owned
record under ``(cluster, canonical workspace, dsh)`` refuses a second launcher
for the same workspace; ``--status`` / ``--cancel-generation`` inspect or stop
it.

Usage::

    .venv/bin/python spawn_dsh.py <workspace-dir> --impersonate-self \\
        --impersonation-name 'Fix login' --brief '<the full briefing text>'

dsh resolves its model credential itself (inherited environment,
``$DSH_HOME/.credentials.yaml``, the workspace ``.env``, then ``$DSH_HOME/.env``).
The session runs with ``DSH_PERMISSION_MODE=danger-full-access``: nobody
answers approval prompts in a PTY takeover, the same hands-off posture as the
Codex and Claude launchers. The launch message and the patch live in the
generation's private state directory; the plugin deletes the message once read.
dsh runs under the PTY shell, so a failed boot is reported with its output.

Output: owner-record fields (``adopted`` / ``session_id`` / ``generation`` …),
one ``key=value`` per line.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import shlex
import shutil
import sys
import time
from pathlib import Path

import ava
from shared import coding_session_owner
from shared.agents import AgentNotFound, AgentStatus

_HERE = Path(__file__).resolve().parent
_PLUGIN = _HERE / "ava-relay-dsh" / "ava-relay.mjs"
_DEFAULT_TTL_SECONDS = 24 * 3600
_READY_MARKER = "dsh takeover session "
_EXITED = re.compile(r"dsh exited with status \d+")
_LAUNCH_FILE = "launch.txt"
_PATCH_FILE = "ava-relay.patch.yml"


def _resolve_dir(dir_path: str) -> Path:
    p = Path(dir_path).expanduser().resolve()
    if not p.is_dir():
        print(f"error: {p} is not a directory or does not exist", file=sys.stderr)
        sys.exit(1)
    return p


def _executables() -> tuple[str, str]:
    """Absolute ``node`` and ``dsh`` paths: the PTY shell's PATH may lack either."""
    node = shutil.which("node")
    dsh = shutil.which("dsh")
    if node is None or dsh is None:
        raise RuntimeError(
            "DeepSeek Harness needs `node` and `dsh` on PATH "
            "(install with `npm install -g @deepseek-ai/dsh`)"
        )
    return node, dsh


def _patch(plugin: Path, launch_file: Path) -> str:
    """Replace the one-shot headless runner with the relay plugin's takeover session.

    JSON strings are valid YAML scalars, so paths need no further quoting.
    """
    return (
        "- id: headless-startup\n"
        "  disabled: true\n"
        "- id: headless-runner\n"
        "  disabled: true\n"
        "- insert:\n"
        "    - id: ava-relay\n"
        f"      name: {json.dumps(str(plugin))}\n"
        "      config:\n"
        f"        takeoverFile: {json.dumps(str(launch_file))}\n"
    )


def _write_private(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def _dsh_command(workspace: Path, node: str, dsh: str, patch: Path) -> str:
    """Run dsh under the PTY shell (no ``exec``) so a failed boot stays readable.

    The exit line is printed through a format string: the echoed command
    itself never matches ``_EXITED``.
    """
    return (
        f"cd {shlex.quote(workspace.as_posix())} && "
        "DSH_PERMISSION_MODE=danger-full-access "
        f"{shlex.quote(node)} {shlex.quote(dsh)} --profile headless "
        f"--patch {shlex.quote(patch.as_posix())}; "
        "printf 'dsh %s with status %s\\n' exited \"$?\""
    )


def _wait_for_session(sid: int, timeout: float = 90.0) -> str:
    """The plugin echoes its session id right before it submits the launch message."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        lines = ava.shell.sessions.capture(sid).splitlines()
        for line in lines:
            if line.startswith(_READY_MARKER):
                return line.removeprefix(_READY_MARKER).split()[0]
            if _EXITED.fullmatch(line.strip()):
                tail = "\n".join(lines[-20:])
                raise RuntimeError(f"dsh exited before its takeover session opened:\n{tail}")
        time.sleep(1)
    raise RuntimeError(f"dsh did not open its takeover session in PTY {sid} within {timeout:.0f} s")


def _takeover_bootstrap_message(agent_id: int, name: str, brief: str) -> str:
    """Inline the briefing; a takeover reads no task or work file."""
    from ava._impersonation_launch import bootstrap_message

    guide = _HERE.parents[3] / ".agents" / "skills" / "impersonator-guide" / "SKILL.md"
    return bootstrap_message(agent_id, name, "dsh", brief, guide)


def _owner_terminated(agent_id: int) -> bool:
    try:
        return ava.agents.get_status(agent_id) is AgentStatus.TERMINATED
    except AgentNotFound:
        return True
    except Exception:
        # An unavailable gateway cannot prove an owner dead; expiry keeps
        # responsibility, and guessing here could kill active work.
        return False


def _print_owner(owner: coding_session_owner.CodingSessionOwner, *, adopted: bool) -> None:
    print(f"adopted={'true' if adopted else 'false'}")
    print(f"status={owner.status}")
    for field in ("generation", "owner_agent_id", "session_id", "session_name", "state_dir"):
        value = getattr(owner, field)
        if value is not None:
            print(f"{field}={value}")


def _status(key: coding_session_owner.CodingSessionKey) -> int:
    owner = coding_session_owner.read(key)
    _print_owner(owner, adopted=False)
    if owner.error:
        print(f"error={owner.error}", file=sys.stderr)
    return 1 if owner.status == "invalid" else 0


def _cancel(key: coding_session_owner.CodingSessionKey, generation: str) -> int:
    if not coding_session_owner.terminate_generation(key, generation, reason="explicit-cancel"):
        print("cancel refused: generation is not the current canonical owner", file=sys.stderr)
        return 1
    _print_owner(coding_session_owner.read(key), adopted=False)
    return 0


def _claim(
    key: coding_session_owner.CodingSessionKey, ttl_seconds: float
) -> coding_session_owner.CodingSessionOwner:
    """Claim a fresh generation, waiting through another claimant's bounded launch."""
    previous = coding_session_owner.read(key)
    terminated = None
    if (
        previous.generation is not None
        and previous.owner_agent_id is not None
        and _owner_terminated(previous.owner_agent_id)
    ):
        terminated = previous.generation
    while True:
        result = coding_session_owner.claim(
            key,
            owner_agent_id=ava.self.AGENT_ID,
            tasks_file=None,
            work_file=None,
            ttl_seconds=ttl_seconds,
            terminated_generation=terminated,
        )
        if result.action == "adopt":
            raise RuntimeError(
                "a takeover needs a fresh coding workspace; this workspace already has a live "
                "generation - cancel it with --cancel-generation first"
            )
        if result.action != "busy":
            return result.owner
        time.sleep(0.25)


def _launch(workspace: Path, name: str, brief: str, ttl_seconds: float) -> int:
    node, dsh = _executables()
    if not _PLUGIN.is_file():
        raise RuntimeError(f"the dsh relay plugin is missing: {_PLUGIN}")
    key = coding_session_owner.canonical_key(workspace, tool="dsh")
    owner = _claim(key, ttl_seconds)
    if (
        owner.generation is None
        or owner.expected_suffix is None
        or owner.owner_agent_id is None
        or owner.state_dir is None
    ):
        raise RuntimeError("new canonical owner is missing launch fields")
    generation = owner.generation
    sid: int | None = None
    try:
        owner.state_dir.mkdir(parents=True, exist_ok=False)
        owner.state_dir.chmod(0o700)
        launch_file = owner.state_dir / _LAUNCH_FILE
        patch = owner.state_dir / _PATCH_FILE
        _write_private(launch_file, _takeover_bootstrap_message(owner.owner_agent_id, name, brief))
        _write_private(patch, _patch(_PLUGIN, launch_file))
        sid = ava.shell.sessions.new(name=owner.expected_suffix, ttl=ttl_seconds)
        active = coding_session_owner.publish_active(
            key,
            generation,
            session_id=sid,
            session_name=coding_session_owner.full_session_name(
                owner.owner_agent_id, sid, owner.expected_suffix
            ),
        )
        ava.shell.sessions.send(sid, _dsh_command(workspace, node, dsh, patch))
        dsh_session = _wait_for_session(sid)
    except BaseException:
        if sid is not None:
            with contextlib.suppress(Exception):
                ava.shell.sessions.kill(sid)
        with contextlib.suppress(Exception):
            coding_session_owner.terminate_generation(key, generation, reason="launch-failed")
        raise
    print(f"ready. name={active.expected_suffix} workspace={workspace}")
    print(f"dsh_session={dsh_session}")
    _print_owner(active, adopted=False)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Launch DeepSeek Harness in a persistent shell session to take over "
        "the launching Ava agent."
    )
    parser.add_argument("workspace", help="Working directory for the dsh session.")
    parser.add_argument(
        "--ttl-seconds",
        type=float,
        default=_DEFAULT_TTL_SECONDS,
        help="Hard expiry of the shell session, up to one day (default: %(default)s).",
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
        help="Takeover briefing text, inlined verbatim into the launch message.",
    )
    args = parser.parse_args()
    if args.impersonate_self:
        from ava._boot import require_agent_id

        require_agent_id()
        if args.status or args.cancel_generation:
            parser.error("--impersonate-self requires a new launch")
        if args.brief is None or not args.brief.strip():
            parser.error("--impersonate-self requires a non-empty --brief")
    elif not (args.status or args.cancel_generation):
        parser.error("dsh runs only as a takeover: pass --impersonate-self with --brief")
    elif args.impersonation_name is not None or args.brief is not None:
        parser.error("--impersonation-name/--brief require --impersonate-self")

    if args.status or args.cancel_generation:
        key = coding_session_owner.canonical_key(
            Path(args.workspace).expanduser().resolve(), tool="dsh"
        )
        return _status(key) if args.status else _cancel(key, args.cancel_generation)
    workspace = _resolve_dir(args.workspace)
    return _launch(
        workspace, args.impersonation_name or workspace.name, args.brief, args.ttl_seconds
    )


if __name__ == "__main__":
    sys.exit(main())

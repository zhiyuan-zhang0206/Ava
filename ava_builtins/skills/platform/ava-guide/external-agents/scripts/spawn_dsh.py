#!/usr/bin/env python3
"""Launch DeepSeek Harness (dsh) in a persistent shell session, or inspect/stop it.

dsh runs headless with the Ava relay plugin patched in
(``ava-relay-dsh/ava-relay.mjs``). The plugin stands in for the one-shot
headless runner: it opens one persistent session, submits the initial prompt,
echoes the session to the PTY, and starts the Ava relay from the credential
stub ``ava impersonate request --provider dsh`` writes. Each launch owns a
generation record of its own under ``(cluster, workspace, dsh)``;
``--status`` / ``--cancel-generation`` inspect or stop them.

Usage::

    .venv/bin/python spawn_dsh.py <workspace-dir> --prompt '<initial prompt>'

dsh resolves its model credential itself (inherited environment,
``$DSH_HOME/.credentials.yaml``, the workspace ``.env``, then ``$DSH_HOME/.env``).
The session runs with ``DSH_PERMISSION_MODE=danger-full-access``: nobody
answers approval prompts in a headless PTY session. The initial prompt and the
patch live in the generation's private state directory; the plugin deletes the
message once read. dsh runs under the PTY shell, so a failed boot is reported
with its output.

Output: owner-record fields (``status`` / ``session_id`` / ``generation`` …),
one ``key=value`` per line.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import sys
import time
from pathlib import Path

import ava
from base.api_contracts.agent_contract import AgentNotFound, AgentStatus, GatewayUnavailable
from base.host.atomic_io import SYSTEM_FS, AtomicWriter
from base.sessions import coding_session_owner
from base.sessions.coding_session_owner_record import CodingSessionStatus

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
    """Replace the one-shot headless runner with the relay plugin's persistent session.

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
                raise RuntimeError(f"dsh exited before its session opened:\n{tail}")
        time.sleep(1)
    raise RuntimeError(f"dsh did not open its session in PTY {sid} within {timeout:.0f} s")


def _owner_terminated(agent_id: int) -> bool:
    try:
        return ava.agents.get_status(agent_id) is AgentStatus.TERMINATED
    except AgentNotFound:
        return True
    except GatewayUnavailable as exc:
        # An unavailable gateway cannot prove an owner dead; expiry keeps
        # responsibility, and guessing here could kill active work.
        print(
            f"gateway unavailable checking owner agent {agent_id}; treating it as alive: {exc!r}",
            file=sys.stderr,
        )
        return False


def _print_owner(owner: coding_session_owner.CodingSessionOwner) -> None:
    print(f"status={owner.status}")
    for field in ("generation", "owner_agent_id", "session_id", "session_name", "state_dir"):
        value = getattr(owner, field)
        if value is not None:
            print(f"{field}={value}")


def _status(key: coding_session_owner.CodingSessionKey) -> int:
    owners = coding_session_owner.list_generations(key)
    if not owners:
        print("status=inactive")
    for index, owner in enumerate(owners):
        if index:
            print()
        _print_owner(owner)
        if owner.error:
            print(f"error={owner.error}", file=sys.stderr)
    return 1 if any(owner.status == CodingSessionStatus.INVALID for owner in owners) else 0


def _cancel(key: coding_session_owner.CodingSessionKey, generation: str) -> int:
    if not coding_session_owner.terminate_generation(
        key, generation, reason="explicit-cancel", writer=AtomicWriter(SYSTEM_FS)
    ):
        print(f"cancel refused: no generation {generation} is recorded here", file=sys.stderr)
        return 1
    _print_owner(coding_session_owner.read(key, generation))
    return 0


def _new_generation(
    key: coding_session_owner.CodingSessionKey, ttl_seconds: float
) -> coding_session_owner.CodingSessionOwner:
    """A fresh generation of our own; dead siblings in the workspace are reclaimed first."""
    return coding_session_owner.launch_generation(
        key,
        owner_agent_id=ava.self.AGENT_ID,
        tasks_file=None,
        work_file=None,
        ttl_seconds=ttl_seconds,
        writer=AtomicWriter(SYSTEM_FS),
        owner_terminated=_owner_terminated,
    )


def _launch(workspace: Path, prompt: str, ttl_seconds: float) -> int:
    node, dsh = _executables()
    if not _PLUGIN.is_file():
        raise RuntimeError(f"the dsh relay plugin is missing: {_PLUGIN}")
    key = coding_session_owner.canonical_key(workspace, tool="dsh")
    owner = _new_generation(key, ttl_seconds)
    if (
        owner.generation is None
        or owner.expected_suffix is None
        or owner.owner_agent_id is None
        or owner.state_dir is None
    ):
        raise RuntimeError("new owner generation is missing launch fields")
    generation = owner.generation
    sid: int | None = None
    try:
        owner.state_dir.mkdir(parents=True, exist_ok=False)
        owner.state_dir.chmod(0o700)
        launch_file = owner.state_dir / _LAUNCH_FILE
        patch = owner.state_dir / _PATCH_FILE
        _write_private(launch_file, prompt)
        _write_private(patch, _patch(_PLUGIN, launch_file))
        sid = ava.shell.sessions.new(name=owner.expected_suffix, ttl=ttl_seconds)
        active = coding_session_owner.publish_active(
            key,
            generation,
            session_id=sid,
            session_name=coding_session_owner.full_session_name(
                owner.owner_agent_id, sid, owner.expected_suffix
            ),
            writer=AtomicWriter(SYSTEM_FS),
        )
        ava.shell.sessions.send(sid, _dsh_command(workspace, node, dsh, patch))
        dsh_session = _wait_for_session(sid)
    except BaseException:
        if sid is not None:
            try:
                ava.shell.sessions.kill(sid)
            except Exception as kill_exc:
                print(f"could not reclaim shell session {sid}: {kill_exc!r}", file=sys.stderr)
        try:
            coding_session_owner.terminate_generation(
                key, generation, reason="launch-failed", writer=AtomicWriter(SYSTEM_FS)
            )
        except Exception as term_exc:
            print(f"could not roll back generation {generation}: {term_exc!r}", file=sys.stderr)
        raise
    print(f"ready. name={active.expected_suffix} workspace={workspace}")
    print(f"dsh_session={dsh_session}")
    _print_owner(active)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Launch DeepSeek Harness in a persistent shell session."
    )
    parser.add_argument("workspace", help="Working directory for the dsh session.")
    parser.add_argument(
        "--ttl-seconds",
        type=float,
        default=_DEFAULT_TTL_SECONDS,
        help="Hard expiry of the shell session, up to one day (default: %(default)s).",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--status", action="store_true", help="Print every owner record of the workspace."
    )
    action.add_argument(
        "--cancel-generation",
        metavar="GENERATION",
        help="Stop and terminalize exactly this generation.",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="Initial prompt text submitted to the new dsh session.",
    )
    args = parser.parse_args()
    if args.status or args.cancel_generation:
        if args.prompt is not None:
            parser.error("--prompt requires a new launch")
        key = coding_session_owner.canonical_key(
            Path(args.workspace).expanduser().resolve(), tool="dsh"
        )
        return _status(key) if args.status else _cancel(key, args.cancel_generation)
    if args.prompt is None or not args.prompt.strip():
        parser.error("new dsh launch requires a non-empty --prompt")
    workspace = _resolve_dir(args.workspace)
    from ava.sdk_surface.agent_identity import require_agent_id

    require_agent_id()
    return _launch(
        workspace,
        args.prompt,
        args.ttl_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())

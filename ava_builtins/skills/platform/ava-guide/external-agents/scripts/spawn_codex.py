#!/usr/bin/env python3
"""Launch, inspect, or stop Codex sessions in persistent shells.

The launch logic lives in ``ava.shell.coding_tools.codex``; this script is its
command-line entry and passes its own skill directory, whose ``references/``
holds the collaboration contract and whose ``scripts/`` holds the supervisor
script (``watch_work.py``). See ``references/canonical_codex_owner.md`` for
the ownership contract and the printed ``key=value`` fields.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import sys
from pathlib import Path

from ava.shell.coding_tools import codex

# This skill's own directory: its references/ holds the collaboration
# contract and locates the impersonator guide, its scripts/ (this file's own
# directory) holds the supervisor script.
_SKILL_DIR = Path(__file__).resolve().parent.parent


def _launch_layout() -> dict[str, Path]:
    if "skill_dir" in inspect.signature(codex.launch).parameters:
        return {"skill_dir": _SKILL_DIR}

    # Keep the old runtime's two files together without changing the managed skill copy.
    key = hashlib.sha256(str(_SKILL_DIR).encode()).hexdigest()[:16]
    reference = Path.home() / ".cache" / "ava" / "skill-spawn-compat" / key / "reference"
    reference.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, target in (
        ("collaboration_protocol.md", _SKILL_DIR / "references" / "collaboration_protocol.md"),
        ("watch_work.py", _SKILL_DIR / "scripts" / "watch_work.py"),
    ):
        link = reference / name
        if link.is_symlink() and link.readlink() == target:
            continue
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            raise RuntimeError(f"compatibility path is occupied: {link}")
        try:
            link.symlink_to(target)
        except FileExistsError:
            if not link.is_symlink() or link.readlink() != target:
                raise
    return {"reference_dir": reference}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch, inspect, or stop Codex sessions (a supervised worker or a takeover)."
    )
    parser.add_argument("workspace", help="Workspace directory for Codex.")
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
        default=codex.DEFAULT_TTL_SECONDS,
        help="Task-adapted hard expiry, up to one day (default: %(default)s).",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--status", action="store_true", help="Print every owner record of the workspace."
    )
    action.add_argument(
        "--resume",
        metavar="SESSION_ID",
        type=codex.session_uuid,
        help="Reopen this recorded Codex session (the codex_session a launch printed) instead of starting a new one; the workspace must have no live generation.",
    )
    action.add_argument(
        "--cancel-generation",
        metavar="GENERATION",
        help="Stop and terminalize exactly this generation.",
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
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.impersonate_self:
        from ava.sdk_surface.agent_identity import require_agent_id

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


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    _validate_args(parser, args)

    workspace = Path(args.workspace).expanduser().resolve()
    if not args.status and not args.cancel_generation:
        workspace = codex.resolve_dir(args.workspace)
    if args.status:
        return codex.status(workspace)
    if args.cancel_generation:
        return codex.cancel(workspace, args.cancel_generation)
    tasks_file = None
    work_file = None
    if not args.impersonate_self:
        tasks_file = codex.resolve_file(workspace, args.tasks_file or "tasks.md")
        work_file = codex.resolve_file(workspace, args.work_file or "work.md")
    return codex.launch(
        workspace,
        tasks_file,
        work_file,
        args.ttl_seconds,
        args.caller_instance,
        (args.impersonation_name or workspace.name) if args.impersonate_self else None,
        args.brief,
        resume=args.resume,
        **_launch_layout(),
    )


if __name__ == "__main__":
    sys.exit(main())

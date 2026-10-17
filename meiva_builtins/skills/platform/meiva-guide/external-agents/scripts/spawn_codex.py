#!/usr/bin/env python3
"""Launch, inspect, or stop Codex sessions in persistent shells.

The launch logic lives in ``meiva.shell.coding_tools.codex``; this script is its
command-line entry and passes its own skill directory, whose ``references/``
holds the collaboration contract and whose ``scripts/`` holds the supervisor
script (``watch_work.py``). Every launch starts a shared ``codex app-server``
endpoint and prints ``codex_app_server`` and ``codex_session`` so the session
can also accept an impersonation handover when instructed via a message.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from meiva.shell.coding_tools import codex

_SKILL_DIR = Path(__file__).resolve().parent.parent


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch, inspect, or stop Codex sessions in persistent shells."
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
        "--prompt",
        default=None,
        help="Initial prompt text sent instead of the two-file collaboration contract.",
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
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.prompt is not None:
        if args.status or args.cancel_generation or args.resume is not None:
            parser.error("--prompt requires a fresh launch")
        if args.tasks_file is not None or args.work_file is not None:
            parser.error("--tasks-file/--work-file cannot be combined with --prompt")
        if not args.prompt.strip():
            parser.error("--prompt must be non-empty")


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
    if args.prompt is None:
        tasks_file = codex.resolve_file(workspace, args.tasks_file or "tasks.md")
        work_file = codex.resolve_file(workspace, args.work_file or "work.md")
    return codex.launch(
        workspace,
        tasks_file,
        work_file,
        args.ttl_seconds,
        args.caller_instance,
        resume=args.resume,
        prompt=args.prompt,
        skill_dir=_SKILL_DIR,
    )


if __name__ == "__main__":
    sys.exit(main())

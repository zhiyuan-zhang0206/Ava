#!/usr/bin/env python3
"""Launch, inspect, or stop Claude Code sessions in persistent shells.

A supervised worker (the default) keeps the two-file collaboration — a task
file, a work file, and a session the launcher's owner follows with
``watch_work.py``. An interactive prompt launch (``--prompt``) starts Claude
Code without task/work files and sends the prompt as its first message. To
have a Claude Code session impersonate an Meiva agent, spawn it normally and send
it a message instructing it to run ``meiva impersonate request --provider claude``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from meiva.shell.coding_tools import claude

_SKILL_DIR = Path(__file__).resolve().parent.parent


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pre-trust a workspace and launch Claude Code in a persistent shell session."
    )
    parser.add_argument(
        "workspace",
        help="Workspace directory for Claude Code. Any directory; no layout is assumed.",
    )
    parser.add_argument(
        "--tasks-file",
        default=None,
        help="Where you append tasks for the agent, absolute or relative to the "
        "workspace (default: tasks.md). Supervised worker mode only.",
    )
    parser.add_argument(
        "--work-file",
        default=None,
        help="Where the agent reports STATUS + log; this is what watch_work.py "
        "polls, absolute or relative to the workspace (default: work.md). "
        "Supervised worker mode only.",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="Initial prompt text sent instead of the two-file collaboration contract.",
    )
    parser.add_argument(
        "--ttl-seconds",
        type=float,
        default=claude.DEFAULT_TTL_SECONDS,
        help="Task-adapted hard expiry, up to one day (default: %(default)s).",
    )
    parser.add_argument(
        "--caller-instance",
        default=None,
        help="opt in to v1 external provenance (bounded instance ID); requires target protocol support",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--status",
        action="store_true",
        help="Print the workspace's owner records.",
    )
    action.add_argument(
        "--resume",
        metavar="SESSION_ID",
        type=claude.session_uuid,
        help="Reopen this recorded Claude Code session (the claude_session a launch printed) instead of starting a new one.",
    )
    action.add_argument(
        "--cancel-generation",
        metavar="GENERATION",
        help="Stop and terminalize exactly this generation.",
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.prompt is not None:
        if args.status or args.cancel_generation:
            parser.error("--prompt requires a launch")
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
        workspace = claude.resolve_dir(args.workspace)
    if args.status:
        return claude.status(workspace)
    if args.cancel_generation:
        return claude.cancel(workspace, args.cancel_generation)
    tasks_file = None
    work_file = None
    if args.prompt is None:
        tasks_file = claude.resolve_file(workspace, args.tasks_file or "tasks.md")
        work_file = claude.resolve_file(workspace, args.work_file or "work.md")
    return claude.launch(
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

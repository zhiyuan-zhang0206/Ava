#!/usr/bin/env python3
"""Launch, message, inspect, or stop Google Antigravity conversations.

The launch and control logic lives in ``meiva.shell.coding_tools.antigravity``;
this script is its command-line entry.

Usage::

    # Supervised worker mode (creates tasks.md + work.md, sends contract)
    .venv/bin/python spawn_antigravity.py <workspace-dir>

    # Interactive prompt mode (starts conversation with initial prompt, no files)
    .venv/bin/python spawn_antigravity.py <workspace-dir> --prompt '<initial prompt>'

    # Send a follow-up message (e.g., new tasks or impersonation instructions)
    .venv/bin/python spawn_antigravity.py <workspace-dir> --send <CONVERSATION_ID> --message '<text>'

    # Inspect or stop a conversation
    .venv/bin/python spawn_antigravity.py <workspace-dir> --status <CONVERSATION_ID>
    .venv/bin/python spawn_antigravity.py <workspace-dir> --stop <CONVERSATION_ID>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from meiva.shell.coding_tools import antigravity

_SKILL_DIR = Path(__file__).resolve().parent.parent


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch, message, inspect, or stop Google Antigravity conversations."
    )
    parser.add_argument(
        "workspace",
        help="Workspace directory for Antigravity.",
    )
    parser.add_argument(
        "--tasks-file",
        default=None,
        help="Task input file, absolute or relative to the workspace (default: tasks.md).",
    )
    parser.add_argument(
        "--work-file",
        default=None,
        help="STATUS and log file, absolute or relative to the workspace (default: work.md).",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="Initial prompt text sent instead of the two-file collaboration contract.",
    )
    parser.add_argument(
        "--message",
        default=None,
        help="Message text for --send ('-' reads stdin).",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--send",
        metavar="CONVERSATION_ID",
        type=antigravity.session_uuid,
        help="Send --message to an existing Antigravity conversation UUID.",
    )
    action.add_argument(
        "--status",
        metavar="CONVERSATION_ID",
        type=antigravity.session_uuid,
        help="Print metadata for an existing Antigravity conversation UUID.",
    )
    action.add_argument(
        "--stop",
        metavar="CONVERSATION_ID",
        type=antigravity.session_uuid,
        help="Force-stop an existing Antigravity conversation tree.",
    )
    return parser


def _has_launch_flags(args: argparse.Namespace) -> bool:
    return bool(
        args.prompt is not None or args.tasks_file is not None or args.work_file is not None
    )


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    non_launch = bool(args.send is not None or args.status is not None or args.stop is not None)
    if args.send is not None and args.message is None:
        parser.error("--send requires --message")
    if args.send is None and args.message is not None:
        parser.error("--message requires --send")
    if non_launch and _has_launch_flags(args):
        parser.error("--send/--status/--stop cannot be combined with launch flags")
    if args.prompt is not None:
        if args.tasks_file is not None or args.work_file is not None:
            parser.error("--tasks-file/--work-file cannot be combined with --prompt")
        if not args.prompt.strip():
            parser.error("--prompt must be non-empty")


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    _validate_args(parser, args)

    if args.send is not None:
        content = sys.stdin.read() if args.message == "-" else args.message
        return antigravity.send(args.send, content)
    if args.status is not None:
        return antigravity.status(args.status)
    if args.stop is not None:
        return antigravity.stop(args.stop)

    workspace = antigravity.resolve_dir(args.workspace)
    tasks_file = None
    work_file = None
    if args.prompt is None:
        tasks_file = antigravity.resolve_file(workspace, args.tasks_file or "tasks.md")
        work_file = antigravity.resolve_file(workspace, args.work_file or "work.md")
    return antigravity.launch(
        workspace,
        tasks_file,
        work_file,
        prompt=args.prompt,
        skill_dir=_SKILL_DIR,
    )


if __name__ == "__main__":
    sys.exit(main())

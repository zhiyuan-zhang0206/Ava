#!/usr/bin/env python3
"""Create, adopt, inspect, or stop the canonical Codex workspace generation.

The launch logic lives in ``ava.shell.coding_tools.codex``; this script is its
command-line entry and passes its own directory, which holds the collaboration
contract and the supervisor script (``watch_work.py``). See
``canonical_codex_owner.md`` beside it for the ownership contract and the
printed ``key=value`` fields.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ava.shell.coding_tools import codex

_REFERENCE = Path(__file__).resolve().parent


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
        default=codex.DEFAULT_TTL_SECONDS,
        help="Task-adapted hard expiry, up to one day (default: %(default)s).",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--status", action="store_true", help="Print the canonical owner record.")
    action.add_argument(
        "--resume",
        metavar="SESSION_ID",
        type=codex.session_uuid,
        help="Reopen this recorded Codex session (the codex_session a launch printed) instead of starting a new one; the workspace must have no live generation.",
    )
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
        from ava.agent_identity import require_agent_id

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
        reference_dir=_REFERENCE,
        resume=args.resume,
    )


if __name__ == "__main__":
    sys.exit(main())

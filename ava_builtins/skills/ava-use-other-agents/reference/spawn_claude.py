#!/usr/bin/env python3
"""Create, adopt, inspect, or stop the canonical Claude Code workspace generation.

The active identity is ``(cluster, canonical workspace, claude)``. A supervised
worker (the default) keeps the two-file collaboration — a task file, a work
file, and a session the launcher's owner follows with ``watch_work.py``. A
takeover (``--impersonate-self``) runs file- and supervisor-less with its
briefing inlined in the launch message: its relay starts with the session
via the bundled ava-relay plugin (resident mode; ``--no-relay-resident``
restores the executor-armed Monitor flow). A concurrent or
cross-agent caller adopts the live record instead of stacking another Claude
process.

Usage::

    .venv/bin/python spawn_claude.py <workspace-dir>
    .venv/bin/python spawn_claude.py <workspace-dir> --work-file notes/progress.md
    .venv/bin/python spawn_claude.py <workspace-dir> --impersonate-self \
        --impersonation-name 'Fix login' --brief '<the full briefing text>'

Supervised mode pre-trusts the directory in ``~/.claude.json`` so no trust
prompt blocks the session, creates the task file / work file if absent (parent
dirs included), creates a persistent shell session named ``claude-<dirname>``
and launches ``claude --dangerously-skip-permissions`` (after ``unset
ANTHROPIC_API_KEY`` to avoid API-key billing trap), then polls
``ava.shell.sessions.capture`` until Claude Code has rendered its UI and sends
the collaboration-contract message — which names both file paths, so the
coding agent is told where they are rather than assuming a layout.

A takeover instead publishes a generation-owned record under the canonical key,
opens the session under the record's name, and sends the inline briefing; it
reads no task or work file, writes nothing in the workspace beyond the trust
flag, and starts no supervisor. ``--status`` / ``--cancel-generation`` inspect
or stop the record (supervised launches stay outside the record plane by
design, so those two report none for them).

The workspace is any directory: a git worktree, a scratch folder, a checkout of
an unrelated project. Supervised mode imposes no structure on it beyond the two
files, and both of those are relocatable via the flags below.

The launch logic lives in ``ava.shell.coding_tools.claude``; this script is
its command-line entry and passes its own directory, which holds the
collaboration contract and the resident relay plugin.

Output: owner-record fields (``adopted`` / ``session_id`` / ``generation`` …)
for a takeover; ``session_id``, ``tasks_file``, ``work_file`` for a supervised
launch (one ``key=value`` per line). Feed ``work_file`` straight into
``watch_work.py``'s ``WORK_FILE``; do not reconstruct the path from a
convention. Interact via ``ava.shell.sessions.send`` /
``ava.shell.sessions.send_keys`` / ``ava.shell.sessions.capture`` /
``ava.shell.sessions.kill``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ava.shell.coding_tools import claude

_REFERENCE = Path(__file__).resolve().parent


def _relay_options(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> tuple[bool, Path | None]:
    """Resolve the resident-relay flags; takeover-only, and the group keeps the two exclusive."""
    if not args.impersonate_self and (
        args.no_relay_resident or args.relay_resident_dir is not None
    ):
        parser.error("--no-relay-resident/--relay-resident-dir require --impersonate-self")
    plugin_dir = Path(args.relay_resident_dir) if args.relay_resident_dir else None
    return not args.no_relay_resident, plugin_dir


def main() -> int:
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
        help="Print the canonical owner record (takeover generations; supervised "
        "launches are not registered).",
    )
    action.add_argument(
        "--cancel-generation",
        metavar="GENERATION",
        help="Stop and terminalize exactly this canonical generation "
        "(takeover generations; supervised launches are not registered).",
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
    relay_action = parser.add_mutually_exclusive_group()
    relay_action.add_argument(
        "--no-relay-resident",
        action="store_true",
        help="Launch the takeover without the session relay plugin; the executor "
        "arms the Claude Monitor relay itself, as before.",
    )
    relay_action.add_argument(
        "--relay-resident-dir",
        default=None,
        help="Override the relay plugin directory loaded into the takeover "
        "(default: the ava-relay directory beside this launcher).",
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
        workspace = claude.resolve_dir(args.workspace)
    if args.status:
        return claude.status(workspace)
    if args.cancel_generation:
        return claude.cancel(workspace, args.cancel_generation)
    tasks_file = None
    work_file = None
    if not args.impersonate_self:
        tasks_file = claude.resolve_file(workspace, args.tasks_file or "tasks.md")
        work_file = claude.resolve_file(workspace, args.work_file or "work.md")
    relay_resident, relay_plugin_dir = _relay_options(parser, args)
    return claude.launch(
        workspace,
        tasks_file,
        work_file,
        args.ttl_seconds,
        args.caller_instance,
        (args.impersonation_name or workspace.name) if args.impersonate_self else None,
        args.brief,
        reference_dir=_REFERENCE,
        relay_resident=relay_resident,
        relay_plugin_dir=relay_plugin_dir,
    )


if __name__ == "__main__":
    sys.exit(main())

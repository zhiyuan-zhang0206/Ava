"""Launch external coding tools (Claude Code, Codex) in persistent shell sessions.

Each tool's launcher (``claude``, ``codex``) opens an ``meiva.shell.sessions``
PTY, starts the tool there, and delivers its first message — a collaboration
contract for a supervised worker, or an inline briefing for a takeover that
impersonates the launching agent. The ``meiva-guide.external-agents`` skill's
``spawn_*.py`` scripts are the command-line entries. Not part of the agent-facing
``meiva.shell`` surface.
"""

from pathlib import Path

import base.paths


def impersonator_guide() -> Path:
    """The executor manual in the checkout owning this Meiva runtime.

    Skill sources and installed copies can have different directory depths;
    the runtime source owner supplies this built-in manual.
    """
    return (
        base.paths.repo_root()
        / "meiva_builtins"
        / "skills"
        / "platform"
        / "impersonator-guide"
        / "SKILL.md"
    )

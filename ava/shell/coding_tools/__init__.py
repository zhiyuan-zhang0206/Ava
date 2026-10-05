"""Launch external coding tools (Claude Code, Codex) in persistent shell sessions.

Each tool's launcher (``claude``, ``codex``) opens an ``ava.shell.sessions``
PTY, starts the tool there, and delivers its first message — a collaboration
contract for a supervised worker, or an inline briefing for a takeover that
impersonates the launching agent. The ``ava-guide.external-agents`` skill's
``spawn_*.py`` scripts are the command-line entries. Not part of the agent-facing
``ava.shell`` surface.
"""

"""Session supervision — native process hosts, their records, and coding-tool sessions.

The session `backend` picks between the POSIX (`posixproc`) and
permissions-helper (`helperproc`) native
process hosts; the client of the pty-sessions service (which holds agent
shells) lives in `pty/`. `record` and
`env_forwarding` are the on-disk session record and the env-handoff mechanism
every host shares; `code_provenance` and `log_prefix` round out a session's
identity. `coding_session_owner` and its record give external coding-tool
sessions (Claude Code, Codex, ...) a generation-owned lifecycle independent of
the native process hosts. `helper_chain_guard` detects processes orphaned from
the permissions helper.
"""

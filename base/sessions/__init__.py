"""Session supervision — native process hosts, their records, and coding-tool sessions.

The cross-platform session `backend` picks between the POSIX (`posixproc`),
Windows (`windows.winproc`) and permissions-helper (`helperproc`) native
process hosts; per-session PTY hosts live in `pty/`. `record` and
`env_forwarding` are the on-disk session record and the env-handoff mechanism
every host shares; `code_provenance` and `log_prefix` round out a session's
identity. `coding_session_owner` and its record give external coding-tool
sessions (Claude Code, Codex, ...) a generation-owned lifecycle independent of
the native process hosts. `helper_chain_guard` detects processes orphaned from
the permissions helper. `windows/` holds Windows-only session machinery
(logon-session identity, the resident steward, terminal resources); its
modules are import-safe on every platform, but the `ctypes.WinDLL` calls
they wrap refuse at call time off Windows.
"""

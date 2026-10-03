"""The pty-sessions service: a machine's agent shells held by one ordinary roster process.

``service`` is the session table and the protocol, ``session`` one live shell,
``ledger`` the file the next start sweeps a crash's leftovers from, ``daemon``
the entry point (``python -m services.pty_sessions.daemon``).
"""

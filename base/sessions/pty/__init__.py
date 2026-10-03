"""PTY sessions — the client side of the pty-sessions service.

Every agent interactive shell lives in the machine's pty-sessions service
(``services/pty_sessions``), an ordinary roster process that holds each session's
pty master and its screen model. A session therefore outlives an agent, an agent
host or a gateway restarting; it ends with its shell, a ``kill``, a stop's
closure, or the service stopping (decisions/2026-10-03-pty-sessions-service.md).

This package is what agent, schedule, page-server and stop processes import:

- ``client.py`` — the unix-socket client and the data types it returns;
- ``protocol.py`` — the JSON-line wire format shared with the service;
- ``paths.py`` — the socket, instance lock, ledger and transcript locations;
- ``keys.py`` — the send-keys key vocabulary, translated to bytes before dialing;
- ``closure.py`` — the one terminal closure (hang up, a grace, SIGKILL), run by
  the service for a stop, a shutdown and the sweep of a crashed service;
- ``session_tree.py`` — the processes a session owns, captured by identity and
  killed whole;
- ``allocation_freeze.py`` — the home's generation-owned admission freeze;
- ``screen.py`` — the pyte wrapper the service renders captures with.
"""

"""PTY sessions — one detached host process per agent interactive shell.

There is no supervisor daemon. Each shell session is carried by its own tiny
host process (``host.py``), double-forked to init at creation, so no infra
process holds every shell's pty master: killing any service — including a
cluster update stopping the world — cannot take a shell down. A session dies
only through its own ``kill`` op, its shell exiting, or that one host
crashing (blast radius: exactly one session). This is what makes the SDK's
"sessions persist across terminate/restart/update" promise structurally true
(decisions/2026-08-13-per-session-pty-hosts.md).

Four modules:

- ``allocation_freeze.py`` — the host-wide, generation-owned marker and
  allocation lock shared by every co-located cluster; operator freeze/resume
  never interrupts an existing session;

- ``host.py`` — the per-session host: owns the ``pty.fork()`` shell, feeds
  its raw bytes into a pyte screen model + ring buffer + byte log, and
  answers session ops over the session's own unix socket
  (``$AVA_HOME/run/pty/<name>.sock``); exits when the session dies;
- ``cli.py`` — the transport contract the SDK consumes (has/new/send/
  send_keys/capture/resize/kill/list/list-started-at), the
  screen-vocabulary key table, and the 0600 envfile writer. ``new`` spawns
  a host; every other session op dials that session's socket;
- ``screen.py`` — the pyte wrapper: incremental UTF-8 decode, raw byte ring
  buffer, screen-parity capture rendering.
"""

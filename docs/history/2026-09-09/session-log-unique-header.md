# Unique session log headers

Filelog identifies files by their leading content, without their paths or
inodes. Separate shell transcripts sharing a login or CLI banner can therefore
collide and be repeatedly rediscovered, amplifying reads. See the earlier
[filelog re-watch storm](../2026-08-24/filelog-rewatch-storm.md).

The host writes a session identity before any child output reaches a newly
created transcript:

```text
--- ava session <name> start=<UTC ISO8601 timestamp> pid=<pid> ---
```

`shared/session_log.py` combines the session name, UTC start time with microsecond
precision, and optional process ID. Session names are not reused within an
agent. Exclusive creation distinguishes new files from existing transcripts;
only new files receive a header. Windows creates its log before spawning the
child, so its header omits the unavailable PID.

The POSIX PTY host counts the new header toward its transcript cap. Reopening
an existing file preserves its content and append behavior, and the cap still
counts only bytes added by that host. Windows passes the header-bearing file
descriptor to `Popen` for stdout and, unless explicitly split, stderr; its
existing handle cleanup remains in place.

This change covers `shared/pty_sessions/host.py` and `shared/winproc.py`.
POSIX service logs in `posixproc.py`, agent main logs, collector configuration,
and filelog include patterns are outside this change. Existing colliding files
are not rewritten and remain subject to retention. The header is an ordinary
log line and is also present when a child produces no output.

Targeted tests check UTF-8 formatting, optional PID, timestamp precision,
identity differences, exclusive creation, append preservation, first-line
ordering, PTY cap accounting, and Windows launch handles. After an authorized
rollout, inspect newly created session transcripts for distinct first lines;
existing collision groups disappear as their files expire under retention.

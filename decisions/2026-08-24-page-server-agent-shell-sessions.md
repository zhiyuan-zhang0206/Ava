# Page-server agent-shell sessions

Every `ava.ui.serve()` page now belongs to a persistent shell session owned by
its serving agent. The page-server daemon creates the session, starts the page
server as its initial command, and relaunches a crashed command in that same
shell. A daemon restart adopts the durable session identity rather than
creating another server for the row.

The page row stores both a per-server health token and the session name. The
token makes restart adoption safe, while the session name gives close, stale
configuration, and occupied-port cleanup an exact lifecycle target. Session
indices come from the agent's shared counter, so page shells remain distinct
from interactive agent shells.

Page server processes remain protected from normal agent-session cleanup, but
the daemon owns their lifecycle: a closed row, a changed port or directory, or
a stale server proven to be ours ends the corresponding page session. Foreign
port occupants are left untouched and retry through the existing backoff path.

# Persistent-shell idle reminders

Persistent shells intentionally survive agent turns and lifecycle restarts, so
automatic cleanup would destroy useful long-running work. The reminder design
keeps ownership with the agent: the system asks, names the exact SDK session id
to close, and never kills a session itself. A reply echoing the retention keyword (the zh word for "keep") turns the
covered sessions into standing exceptions until they end.

Terminal output silence was rejected as the primary idle signal because a
quiet foreground job can be busy while a background job can coexist with an
available prompt. The PTY foreground process group answers the actual question:
the shell owning the tty means the prompt is available. Last output remains a
secondary clock that distinguishes a newly returned prompt from an unchanged
idle period.

Reminder state stays in one atomic JSON snapshot under the unit home rather
than adding a database table. The state is machine-local, keyed by machine-local
PTY names, and has no cross-machine query requirement. Reminders merge per
owner per tick so several due shells cause one agent turn and one reply anchor,
not a burst of independent wakeups.

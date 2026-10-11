"""Child process for the shell-sessions identity test: list sessions or report the refusal."""

from ava.shell import sessions

try:
    print("LIST=" + repr(sessions.list()))
except RuntimeError as exc:
    print("REFUSED=" + str(exc))

from meiva.shell import sessions

try:
    print("LIST=" + repr(sessions.list()))
except RuntimeError as exc:
    print("REFUSED=" + str(exc))

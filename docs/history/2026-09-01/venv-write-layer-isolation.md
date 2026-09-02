---
title: Editable venv write-layer isolation
---

# Editable venv write-layer isolation

Worktree sync now fails before uv receives an unsafe target: the preflight
rejects a symlinked or external `.venv`, an external `VIRTUAL_ENV`, and editable
records that name a different checkout. The installer and worktree bootstrap
invoke that preflight and clear `VIRTUAL_ENV` for their syncs.

The production defense belongs at the writable directory boundary because uv
atomically replaces editable records. Converge repairs the records first, then
makes their POSIX site-packages directories `0555`. The existing repair and
production-sync windows temporarily restore owner write access and preserve the
exact original modes on exit.

Each `execute_code` request also checks the interpreter that will spawn its
child. A poison finding is repaired, surfaced once as a retryable structured
error, and prevents any request artifact or child process; a repair failure is
explicitly non-retryable. The check remains uncached so a later poisoned record
cannot bypass it.

The interpreter guard derives the checkout from the lexical `sys.executable`
venv path before resolving the checkout root. Resolving the Python executable
first would follow the normal virtualenv symlink to its base interpreter and
would incorrectly disable the guard. Multiple `.pth` entries are accepted only
when every entry names the same allowed checkout, which accommodates repeated
equivalent paths without accepting a foreign import path.

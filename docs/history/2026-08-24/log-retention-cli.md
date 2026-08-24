# Explicit local log retention

## Context

The filelog re-watch storm established that thousands of dead agent and shell
files can turn a content-fingerprint collision into sustained filesystem work.
The initial containment added a PTY-host startup pass that deleted every
top-level `*.out.log` older than seven days. That pass was broader than the
incident's managed file set, could unlink a file still held open by a live
process, and made starting an unrelated shell the implicit scheduler for log
retention.

## Decision

Local deletion is owned only by `ava logs retention`, intended as a daily
per-machine OS job. The command uses a conservative 14-day default until the
separate rotation direction permits alignment with Loki's seven-day window.
It admits only agent-main stdout, named PTY transcript/host files, and the
observed Loguru rotation shape; scans only the logs-directory root; rejects
symlinks; and excludes paths reported by `psutil.open_files()`.

Dry-run and delete use the same candidate set. Dry-run reports every path,
UTC mtime, size, and total bytes without mutation. Delete continues after an
individual failure, reports that path on stderr, and returns nonzero if any
inspection or deletion failed.

The PTY-startup cleanup, lock, and stamp were removed rather than retained as
a second policy. Existing stamp files are harmless residue and need no
migration.

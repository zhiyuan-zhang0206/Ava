# Family-tiered local log retention

## Context

The explicit retention command already limited cleanup to top-level managed
files and protected files open in visible processes, but applied one global
threshold to every admitted log. The local service logs are not equally useful:
gateway, ops, and watchdog diagnostics are the failure evidence needed after a
longer interval, while shell transcripts and routine service rotations consume
space more quickly.

## Decision

`ava logs retention --family-days` selects a C-style age policy: agent 15d,
named PTY shell 7d, gateway / ops / watchdog 30d, and other managed service
rotations 3d. Its dry-run records the selected family and days for every
candidate, then reports per-family candidate counts and bytes. The flag is
mutually exclusive with the existing `--older-than` global override.

No age flag retains the prior `AVA_LOG_RETENTION_DAYS` behavior with its 14d
fallback, so an existing deployment changes only when its scheduled payload is
updated. The Loguru filename allowlist now also accepts underscore service names
emitted by Ava, including `delivery_watchdog`; this remains an exact rotated-log
shape, not a broad file deletion rule.

This extends [the original explicit-retention decision](../2026-08-24/log-retention-cli.md)
without replacing its safety boundaries.

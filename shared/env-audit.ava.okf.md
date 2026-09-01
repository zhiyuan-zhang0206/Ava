---
type: doc
title: Shared — .env write audit & integrity guard
description: shared/env_audit.py — owner-only JSONL history of official .env writes (site, pid, process, redacted command line, key names, digest; values never recorded) and a read-boundary guard that surfaces out-of-band modifications as an env_unauthorized_write anomaly.
tags: []
---

# Shared — .env write audit & integrity guard

- **`.env` write audit + guard** (`shared/env_audit.py`): every post-bootstrap official `.env`
  write records an owner-only (0600) JSONL history entry — timestamp, site, pid, process, redacted
  command line (executable only), the key NAMES written/removed, the post-write key set, and the
  post-write sha256 digest. Configuration VALUES never enter the history or the event stream.
  `check_env_integrity()` is the guard: armed once a history exists (fresh install/enroll bootstrap
  files are unarmed by design), it takes the same `.env` lock as the writers, compares the current
  digest, and on mismatch appends one self-rate-limited `unauthorized` record, emits
  `env_unauthorized_write` (audit/anomaly), and logs an error. Writers record via
  `runtime_config.write_fields(audit_site=...)`, `rename_env_keys`, `migrate_skip_alias_env_keys`
  and the CLI converge/secret-rotation scripts; the guard runs at the gateway config read boundary
  (`GET /api/config`).

Parent: [[shared/shared.ava.okf.md|shared libraries]].

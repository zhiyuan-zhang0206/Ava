---
type: doc
title: Shared — .env write audit & integrity guard
description: shared/env_audit.py — owner-only JSONL history of official .env writes (ts, site, pid, process, redacted command line, actor, trace id, key names, digest; old/new values for non-sensitive fields only) and a read-boundary guard that surfaces out-of-band modifications as an env_unauthorized_write anomaly.
tags: []
---

# Shared — .env write audit & integrity guard

- **`.env` write audit** (`shared/env_audit.py`): every post-bootstrap official `.env` write records
  an owner-only (0600) JSONL history entry. Record v2 (2026-09-16, task #3588; additive — older
  records simply lack the new fields) carries: timestamp, site, pid, process, redacted command line
  (executable only); the initiating credential fact — `actor` (`user_session:<subject>` /
  `cluster_bearer:<subject>` / `cli:<os-user>`; null where no identity exists, e.g. converge or
  rotation scripts) and `trace_id` when an HTTP request carried one; the key NAMES written/removed;
  the post-write key set; the post-write sha256 digest; and `changed` — the old→new value diff.
  **Values are recorded only for aliases registered with `sensitive: false`**; a sensitive or
  unregistered alias keeps its name with `old`/`new` null, and a metadata-lookup failure withholds
  every value (fail closed). Writers capture the pre-write values under the env lock and hand the
  raw diff to `record_env_write`, which owns the redaction: `runtime_config.write_fields`,
  `envfile.upsert_env` / `envfile.remove_env` (actor + diff) and `envfile.replace_env_bytes_cas`
  (diff only); the rename/migration helpers record the diff-less form. The `env_write` event stream
  entry gains the `actor` and stays value-free — sensitive values enter neither the record nor the
  stream. Motivation: the `AVA_HOST_MAX_CONCURRENT_TURNS=50` write of 2026-09-11 (audit record #18:
  site + key names only) could not be attributed or reconstructed without ssh.
- **Integrity guard**: the first official write also creates a sibling owner-only
  `.env.audit.armed` marker, so deletion, emptiness, corruption, or a missing digest in an armed
  history is reported and rebuilt rather than silently returning to the fresh-home state.
  `check_env_integrity()` is the guard: a fresh install/enrollment has neither marker nor history
  and remains unarmed by design; otherwise the guard takes the same `.env` lock as the writers,
  compares the current digest, and on mismatch appends one self-rate-limited `unauthorized` record,
  emits `env_unauthorized_write` (audit/anomaly), and logs an error. The guard runs at the gateway
  config read boundary (`GET /api/config`).

Parent: [[shared/shared.ava.okf.md|shared libraries]].

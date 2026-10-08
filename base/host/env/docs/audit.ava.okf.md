---
type: doc
title: Base — .env write audit & integrity guard
description: base/host/env/audit.py — owner-only JSONL history of official .env writes (ts, site, pid, process, redacted command line, actor, trace id, key names, digest; old/new values for non-sensitive fields only) and a read-boundary guard that surfaces out-of-band modifications as an env_unauthorized_write anomaly.
tags: []
---

# Base — .env write audit & integrity guard

- **`.env` write audit** (`base/host/env/audit.py`): every post-bootstrap official `.env` write records
  an owner-only (0600) JSONL history entry. Record v2 (2026-09-16, task #3588; additive — older
  records simply lack the new fields) carries: timestamp, site, pid, process, redacted command line
  (executable only); the initiating credential fact — `actor` (`user_session:<subject>` /
  `cluster_bearer:<subject>` / `cli:<os-user>`; null where no identity exists, e.g. converge or
  rotation scripts) and `trace_id` when an HTTP request carried one; the key NAMES written/removed;
  the post-write key set; the post-write sha256 digest; and `changed` — the old→new value diff.
  **Values are recorded only for aliases registered with `sensitive: false`**; a sensitive or
  unregistered alias keeps its name with `old`/`new` null. Alias, scope and sensitivity
  come directly from the existing field declarations in `config_registry`; audit preparation
  does not load panel metadata, current Settings values or a separate metadata cache.
  Invalid declarations are reported and raised, rather than becoming a names-only record.
  The three official writers (`runtime_config.write_fields`, `dotenv_file.upsert_env` and
  `dotenv_file.remove_env`) prepare the redacted diff under their existing env lock **before**
  snapshotting or changing the file. No-op detection and stale-digest checks precede preparation.
  `record_env_write` consumes this prepared diff without reloading metadata. Direct callers may
  still supply a raw diff, which the recorder redacts; those calls describe already-landed bytes
  and cannot roll them back. Diff-less calls retain the names-only form. This ordering detects
  declaration errors before an official write; it does not make the env file and audit history
  crash-atomic. A write whose rendered
  bytes equal the file on disk is not a write: `upsert_env` skips it entirely — no snapshot, no
  rewrite, no record (task #3637: repeated converge runs and boot-retry storms stop manufacturing
  `old == new` records; a quoted or oddly-spaced line still rewrites and normalizes on its first
  differing byte). The `env_write` event stream
  entry gains the `actor` and stays value-free — sensitive values enter neither the record nor the
  stream. Motivation: the `AVA_HOST_MAX_CONCURRENT_TURNS=50` write of 2026-09-11 (audit record #18:
  site + key names only) could not be attributed or reconstructed without ssh.
- **Query surface**: `ava config audit [--last N] [--key K] [--machine M]` reads this unit's
  history by default (no gateway round-trip); `--machine <name|all>` goes through the gateway.
  `GET /api/config/audit?machine=<name|all>&last=<1..200, default 20>` merges newest-first: the
  gateway's own box plus, for `all`, every agent-runner (records tagged with their `machine`;
  an unreachable machine 503s the whole read — fail-fast). Records are the raw JSONL entries, so
  the redaction rules above travel with them.
- **Integrity guard**: the first official CHANGING write also creates a sibling owner-only
  `.env.audit.armed` marker (a no-op upsert neither records nor arms — with the byte-level skip it
  is not a write), so deletion, emptiness, corruption, or a missing digest in an armed
  history is reported and rebuilt rather than silently returning to the fresh-home state.
  `check_env_integrity()` is the guard: a freshly born or joined home has neither marker nor history
  and remains unarmed by design; otherwise the guard takes the same `.env` lock as the writers,
  compares the current digest, and on mismatch appends one self-rate-limited `unauthorized` record,
  emits `env_unauthorized_write` (audit/anomaly), and logs an error. The guard runs at the gateway
  config read boundary (`GET /api/config`).

Parent: [[base/docs/base.ava.okf.md|base library]].

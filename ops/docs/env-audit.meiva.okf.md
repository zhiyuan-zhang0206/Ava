---
type: doc
title: Base — .env write audit & integrity guard
description: ops/env_audit.py — owner-only JSONL history of official .env writes (ts, site, pid, process, redacted command line, actor, trace id, key names, digest; old/new values for non-sensitive fields only) and a read-boundary guard that surfaces out-of-band modifications as an env_unauthorized_write anomaly.
tags: []
---

# Base — .env write audit & integrity guard

- **`.env` write audit** (`ops/env_audit.py`): every post-bootstrap official `.env` write records
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
  stream. Motivation: the `MEIVA_HOST_MAX_CONCURRENT_TURNS=50` write of 2026-09-11 (audit record #18:
  site + key names only) could not be attributed or reconstructed without ssh.
- **Query surface**: `meiva config audit [--last N] [--key K] [--machine M]` reads this unit's
  history by default (no gateway round-trip); `--machine <name|all>` goes through the gateway.
  `GET /api/config/audit?machine=<name|all>&last=<1..200, default 20>` merges newest-first: the
  gateway's own box plus, for `all`, every agent-runner (records tagged with their `machine`;
  an unreachable machine 503s the whole read — fail-fast). Records are the raw JSONL entries, so
  the redaction rules above travel with them.
- **Integrity guard**: the first official CHANGING write also creates a sibling owner-only
  `.env.audit.armed` marker (a no-op upsert neither records nor arms — with the byte-level skip it
  is not a write), so deletion, emptiness, corruption, or a missing digest in an armed
  history is reported and rebuilt rather than silently returning to the fresh-home state.
  `check_env_integrity(machine_reader=...)` borrows the actual read-boundary
  owner's name reader; it resolves identity only when recording a violation.
  Ordinary writes and audit preparation do not resolve identity. The guard: a freshly born or joined home has neither marker nor history
  and remains unarmed by design; otherwise the guard takes the same `.env` lock as the writers,
  compares the current digest, and on mismatch appends one self-rate-limited `unauthorized` record,
  emits `env_unauthorized_write` (audit/anomaly), and logs an error. The guard runs at the gateway
  config read boundary (`GET /api/config`).

## Spawn, snapshot model vision, and spawner validation contract

```meiva-contract
id = "spawn-and-snapshot-contract"
kind = "invariant"
code = [
    "ops/agents/tests/test_agents_internals.py:<module>",
]
tests = [
    "ops/agents/tests/test_agents_internals.py::TestSpawnAgent::test_snapshot_reports_effective_model_vision_support",
    "ops/agents/tests/test_agents_internals.py::TestSpawnAgent::test_snapshot_effective_model_follows_the_birth_stamp",
]
statement = """
`ops/agents/tests/test_agents_internals.py` verifies durable birth configuration,
lifecycle messages, snapshot effective-model vision support, and caller spawner
validation against a real database:
- The create+launch split (`create_agent_row` + `publish_inbound_wake`) inserts
  `agents` (`status='idling'`, `spawner='user'`, `pid=None`) and `agents_meta`
  without inserting an inbound notification row.
- Birth-time lineage is stamped into both `spawner` and `born_spawner`, and a
  spawner-assigned label is stored with `label_user_set=TRUE` so the labeler's
  CAS (`WHERE label IS NULL AND NOT label_user_set`) skips it while an unlabeled
  spawn stays `NULL` + `FALSE`.
- `spawn_agent(config={...})` persists `agents_meta.config_overlay` and
  `select_one` resolves `supports_vision` from the effective model (overlay over
  birth stamp over live default, after `unavailable_fallback` resolution),
  returning `True` for vision-capable models (`claude-sonnet-5`) and `False` for
  text-only models (`glm-5.3`) or withdrawn vision models falling back to a
  text-only model.
- `insert_checkpoint` directly inserts a simplified LangGraph checkpoint row
  (`channel_versions` and `compact_boundary`) bypassing `PostgresSaver`
  serialization overhead, and `TestSpawnerValidation` verifies that malformed
  `agent:` spawner values (`agent:None`, `agent:`, `agent:abc`, `agent:0`,
  `agent:-1`) raise `ValueError` while valid `agent:<positive-id>`, `user`, and
  external spawner strings succeed.
"""
```

Parent: [[base/docs/base.meiva.okf.md|base library]].

---
type: doc
title: Path Lock Discipline
description: The serialization contract for .env and installed.json rewrites — sibling file locks at every door, the leaves-must-stay-leaves rule, and why atomic save alone is not enough.
tags:
- base
- paths
- locks
---

# Path Lock Discipline

- **`.env` rewrites are serialized across processes — at every door.** There are
  four: `runtime_config.write_fields` (the config panel / ops `config_write`),
  `dotenv_file.upsert_env` (first-start identity and development converge),
  `dotenv_file.remove_env`, and `runtime_config.rename_env_keys`. Each holds `base/native_process/os_platform.py:file_lock` on the sibling `.env.lock`
  (`dotenv_file.env_lock_path`) for its whole read-modify-write, with a bounded wait —
  `LockTimeoutError` on expiry rather than writing unsynchronized. Locking one door
  orders nothing: the interleave that matters is converge's `upsert_env` against the
  gateway's or the ops daemon's `write_fields`.
- The doors are **leaves and must stay leaves** — none may call another. `fcntl`
  locks are per open file description, so a nested take blocks on itself and
  surfaces only as a `LockTimeoutError` after the full bound. `snapshot_env` is
  deliberately lock-free for the same reason: it is called from inside them.
- The lock file is a SIBLING because `file_lock`'s POSIX branch opens its path with
  `"w"`, which truncates — pointed at `.env` it would empty the secrets it guards.
  In-process thread safety is separate (`services/agent_ops/dispatch_sync.py:_state_write_lock`);
  neither substitutes for the other.

- **`installed.json` rewrites are serialized the same way**, on the sibling
  `installed.json.lock` (`base/packages/extensions/install_registry.py:registry_lock`). Its writers are
  `ava skill install` in an agent's shell, `ava converge` on a restart, and the gateway's
  skills-toggle handler; every one of
  them is a load-modify-save, and `save` is a full replace, so the same interleave
  drops a package's row while its directory stays on disk. Saving atomically (temp +
  rename) prevents a torn file and does nothing about a lost update.


Parent: [[base/paths/docs/paths.ava.okf.md|paths]].

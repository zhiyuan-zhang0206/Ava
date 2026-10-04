"""Config file accessor — the unit's `$AVA_HOME/.env`.

Every `Settings` field maps to one `.env` key (its env alias). A process reads
`.env` at startup (`base/dotenv_boot.load_ava_env` -> `load_dotenv`) into the
environment, and pydantic-settings builds `Settings` from it; precedence is
`env(.env) > Field default`, with no override layer in between.

Since the 2026-08-01 config refactor, `.env` is the single source of truth only
on a gateway-capable unit. A pure agent-runner's `.env` holds just the bootstrap
env (gateway URL + identity); its cluster-scope fields arrive at Settings build
via the gateway fetch (`base.host.env.bootstrap.inject_config_from_gateway`), which
overrides env/.env for the fetched keys. This module's writes always target the
unit's own `.env` — on a runner that is the bootstrap env plus any host-scope
fields, never the cluster's config.

Writes go straight to `.env` (`set_key` / `unset_key` by alias), so the file
stays the one place a value lives. A change takes effect when the process that
consumes it next (re)starts and re-reads `.env`; the API surface reports which
processes that is (`restart_required`) and never restarts anything itself.

`scope` decides WHICH unit's `.env` a field is written to:

- `cluster-pinned` / `cluster-default` -> the gateway's `.env`. Agent-runners
  and agents read these by fetching `GET /api/bootstrap` at their own startup,
  so the gateway's file is the single cluster-wide copy.
- `host` -> the target machine's own `.env` (per-machine; the gateway proposes a
  write over the ops RPC and the host disposes).
- `agent` -> never in `.env`; a per-agent overlay set at spawn / restart.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast, get_origin

from dotenv import dotenv_values, set_key, unset_key

from base.host.env.dotenv_file import (
    ENV_LOCK_TIMEOUT_S,
    env_lock_path,
    snapshot_env,
)
from base.native_process.os_platform import file_lock

_log = logging.getLogger(__name__)


def _ava_home() -> Path:
    """$AVA_HOME directory, created if missing.

    Routed through `dotenv_boot.resolve_ava_home()`: this module's callers run
    before `load_ava_env` pins `AVA_HOME` into the environment (the
    Settings-lite maintenance commands defer it — see
    `base.host.env.bootstrap._serve_flag`)."""
    from base.host.env.dotenv_boot import resolve_ava_home

    root = resolve_ava_home()
    root.mkdir(parents=True, exist_ok=True)
    return root


def env_file_path() -> Path:
    """This unit's `.env` — the single config source of truth."""
    return _ava_home() / ".env"


def _field_alias_map() -> dict[str, str]:
    """`Settings` field name -> its `.env` alias (the key the field reads from env).

    Lazy import: `base/config` builds on this module's primitives, so a
    top-level import risks a cycle; by call time the config package is built.
    """
    from base.config import field_alias_map

    return field_alias_map()


def _field_is_list(name: str) -> bool:
    """Whether ``name`` is declared as a list-backed Settings field."""
    from base.config import FIELD_INFOS

    return get_origin(FIELD_INFOS[name].annotation) is list


def read_env_aliases() -> dict[str, str]:
    """Raw `{ALIAS: value}` currently set in this unit's `.env` file.

    Reads the FILE (not `os.environ`), so a value written since process start is
    reflected — the gateway can serve a freshly-edited value without restarting
    itself. Absent file -> `{}`. A bare key with no value is dropped.
    """
    path = env_file_path()
    if not path.exists():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def env_set_field_names() -> set[str]:
    """`Settings` field names whose alias is explicitly set in this unit's `.env`."""
    present = set(read_env_aliases())
    return {name for name, alias in _field_alias_map().items() if alias in present}


def env_value_text(value: Any) -> str:
    """Render a config value as the `.env` text pydantic-settings will parse back.

    The single source for the env-string form, shared by the `.env` writer and the
    bootstrap payload so a value round-trips identically through either path.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        # NoDecode comma-list fields (e.g. skills_to_inject) read .env as "a,b" and
        # split on comma; write the same shape so a list value round-trips cleanly.
        return ",".join(str(item) for item in cast("list[Any] | tuple[Any, ...]", value))
    return str(value)


def _pending_changes(
    path: Path, amap: dict[str, str], updates: dict[str, Any], removals: set[str]
) -> list[dict[str, str | None]]:
    """The pre-write audit diff of one `write_fields` call, sorted by alias.

    Captured before the file is rewritten in place. `_audit_changes` (the record
    helper) drops values for everything but `sensitive: false` fields.
    """
    from base.host.env.audit import env_values_from_text

    before = env_values_from_text(
        path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    )
    pending: list[dict[str, str | None]] = [
        {"alias": amap[name], "old": before.get(amap[name]), "new": env_value_text(value)}
        for name, value in updates.items()
    ]
    pending += [
        {"alias": amap[name], "old": before.get(amap[name]), "new": None} for name in removals
    ]
    pending.sort(key=lambda change: str(change["alias"]))
    return pending


def _pin_private(path: Path) -> None:
    # Inside the lock: `.env` is the only on-disk copy of a cluster's secrets
    # — a fresh file (or a dotenv rewrite of one) inherits the umask default
    # (0644 under 022), readable by every user on the box, while its backups
    # are 0600 (snapshot_env). Pin the live file to 0600 like the backups
    # before anything else may observe it, rather than leaving a window where
    # a rewritten file sits world-readable. Best-effort: a chmod failure must
    # not block the config write it follows.
    try:
        path.chmod(0o600)
    except OSError:
        _log.warning("write_fields: could not chmod 0600 %s", path, exc_info=True)


def _check_digest(path: Path, expected_digest: str | None) -> None:
    if expected_digest is None:
        return
    import hashlib

    current = path.read_bytes() if path.exists() else b""
    if hashlib.sha256(current).hexdigest() != expected_digest:
        raise RuntimeError(".env changed before owned runtime-config write")


def _rewrite_env(
    path: Path, amap: dict[str, str], updates: dict[str, Any], removals: set[str]
) -> None:
    """Snapshot the `.env`, apply the updates and removals, and pin it private."""
    snapshot_env(path)
    path.touch(exist_ok=True)
    sp = str(path)
    for name, value in updates.items():
        quote_mode = "never" if _field_is_list(name) else "always"
        set_key(sp, amap[name], env_value_text(value), quote_mode=quote_mode)
    for name in removals:
        unset_key(sp, amap[name])
    _pin_private(path)


def write_fields(
    updates: dict[str, Any],
    removals: set[str],
    *,
    capture_bytes: bool = False,
    expected_digest: str | None = None,
    audit_site: str | None = None,
    actor: str | None = None,
    trace_id: str | None = None,
) -> bytes | None:
    """Set each field's alias in this unit's `.env`, and unset each removed field's.

    `updates` is field name -> value (stringified); `removals` reverts those
    fields to their `Field` default by dropping the `.env` key. Each removal is an
    EXPLICIT drop the caller asked for — callers never infer removals from a key's
    absence, so a partial write can't silently unset a key it didn't mention. The
    `.env` is snapshotted before the write (recoverable) and every unset is logged
    (never silent — a silent full-replace once dropped a cluster's secrets).
    `actor` / `trace_id` describe the initiator for the write audit (the `.env`
    record and the `env_write` event) whenever `audit_site` is set.
    """
    path = env_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Cross-process exclusive, for the whole snapshot-and-rewrite section. Each
    # `set_key` / `unset_key` READS the file and rewrites it, so two writers
    # interleaving means the later one lands carrying the earlier one's snapshot
    # and the earlier one's keys are gone — silently, in the only on-disk copy of
    # a cluster's secrets. The writers are separate PROCESSES (a CLI converge, the
    # gateway's config PUT, the ops daemon's `config_write` arm), so no in-process
    # lock can order them; `services/agent_runner/agent_ops/dispatch_sync.py:_state_write_lock` is the
    # same guarantee for the threads inside one of them, and neither substitutes
    # for the other.
    #
    # It also covers `snapshot_env`: the backup is what recovery reads, and one
    # taken mid-rewrite would preserve a state that never existed.
    captured: bytes | None = None
    with file_lock(env_lock_path(path), timeout_s=ENV_LOCK_TIMEOUT_S):
        if not updates and not removals:
            return path.read_bytes() if capture_bytes and path.exists() else None
        amap = _field_alias_map()
        _check_digest(path, expected_digest)
        changes = (
            _pending_changes(path, amap, updates, removals) if audit_site is not None else None
        )
        _rewrite_env(path, amap, updates, removals)
        if audit_site is not None:
            # Lazy import avoids a module cycle: the audit helper resolves this
            # module's env_file_path only after this writer has acquired the lock.
            from base.host.env.audit import record_env_write

            record_env_write(
                path,
                {amap[name] for name in updates},
                {amap[name] for name in removals},
                site=audit_site,
                actor=actor,
                trace_id=trace_id,
                changes=changes,
            )
        if capture_bytes:
            captured = path.read_bytes()
    if removals:
        _log.info("write_fields: unset %s in %s", sorted(amap[n] for n in removals), path.name)
    return captured

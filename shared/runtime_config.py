"""Config file accessor — the unit's `$AVA_HOME/.env`.

Every `Settings` field maps to one `.env` key (its env alias). A process reads
`.env` at startup (`shared/dotenv_boot.load_ava_env` -> `load_dotenv`) into the
environment, and pydantic-settings builds `Settings` from it; precedence is
`env(.env) > Field default`, with no override layer in between.

Since the 2026-08-01 config refactor, `.env` is the single source of truth only
on a gateway-capable unit. A pure agent-runner's `.env` holds just the bootstrap
env (gateway URL + identity); its cluster-scope fields arrive at Settings build
via the gateway fetch (`shared.bootstrap.inject_config_from_gateway`), which
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

`migrate_host_json_to_env` retires the former per-machine host override file
(`runtime_config.json`) into `.env`, run in the converge phase (cli, before any
process reads `.env`). It is precedence-correct (a key already present in `.env`
is left untouched — env already won over the override) and idempotent (the source
file is archived after the copy). The cluster-wide DB override table that this
once mirrored has been retired (migrated into `.env` and dropped).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, cast, get_origin

from dotenv import dotenv_values, set_key, unset_key

from shared.envfile import ENV_LOCK_TIMEOUT_S, env_lock_path, snapshot_env
from shared.platform import file_lock

_log = logging.getLogger(__name__)


def _ava_home() -> Path:
    """$AVA_HOME directory, created if missing."""
    root = Path(os.environ.get("AVA_HOME", Path.home() / ".ava")).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def env_file_path() -> Path:
    """This unit's `.env` — the single config source of truth."""
    return _ava_home() / ".env"


def _field_alias_map() -> dict[str, str]:
    """`Settings` field name -> its `.env` alias (the key the field reads from env).

    Lazy import: `shared/config` builds on this module's primitives, so a
    top-level import risks a cycle; by call time the config package is built.
    """
    from shared.config import field_alias_map

    return field_alias_map()


def _field_is_list(name: str) -> bool:
    """Whether ``name`` is declared as a list-backed Settings field."""
    from shared.config import FIELD_INFOS

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


def write_fields(updates: dict[str, Any], removals: set[str]) -> None:
    """Set each field's alias in this unit's `.env`, and unset each removed field's.

    `updates` is field name -> value (stringified); `removals` reverts those
    fields to their `Field` default by dropping the `.env` key. Each removal is an
    EXPLICIT drop the caller asked for — callers never infer removals from a key's
    absence, so a partial write can't silently unset a key it didn't mention. The
    `.env` is snapshotted before the write (recoverable) and every unset is logged
    (never silent — a silent full-replace once dropped a cluster's secrets).
    """
    amap = _field_alias_map()
    path = env_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Cross-process exclusive, for the whole snapshot-and-rewrite section. Each
    # `set_key` / `unset_key` READS the file and rewrites it, so two writers
    # interleaving means the later one lands carrying the earlier one's snapshot
    # and the earlier one's keys are gone — silently, in the only on-disk copy of
    # a cluster's secrets. The writers are separate PROCESSES (a CLI converge, the
    # gateway's config PUT, the ops daemon's `config_write` arm), so no in-process
    # lock can order them; `services/agent_ops/daemon.py:_state_write_lock` is the
    # same guarantee for the threads inside one of them, and neither substitutes
    # for the other.
    #
    # It also covers `snapshot_env`: the backup is what recovery reads, and one
    # taken mid-rewrite would preserve a state that never existed.
    with file_lock(env_lock_path(path), timeout_s=ENV_LOCK_TIMEOUT_S):
        snapshot_env(path)
        path.touch(exist_ok=True)
        sp = str(path)
        for name, value in updates.items():
            quote_mode = "never" if _field_is_list(name) else "always"
            set_key(sp, amap[name], env_value_text(value), quote_mode=quote_mode)
        for name in removals:
            unset_key(sp, amap[name])
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
    if removals:
        _log.info("write_fields: unset %s in %s", sorted(amap[n] for n in removals), path.name)


# -- one-time host-file migration: runtime_config.json -> .env (run in converge) --

# Value types env_value_text renders into a .env line the recipient re-parses:
# scalars (bool via its int subclass), and flat lists/tuples of them (comma-joined).
_ENV_PRIMITIVE = (str, int, float, bool)


def _ensure_migratable(name: str, value: Any) -> None:
    """Reject a legacy override value ``env_value_text`` would ``str()`` into a
    corrupt .env line.

    A dict / None / nested container is a shape pydantic-settings cannot re-parse
    from its stringified form, so migrating it would silently write a broken value;
    fail loud instead of corrupting the field.
    """
    if isinstance(value, _ENV_PRIMITIVE):
        return
    if isinstance(value, (list, tuple)) and all(
        isinstance(v, _ENV_PRIMITIVE) for v in cast("list[Any] | tuple[Any, ...]", value)
    ):
        return
    bad = cast("object", value)
    raise TypeError(
        f"host override {name!r} has non-primitive value of type {type(bad).__name__}; "
        "only scalars and flat scalar lists can migrate into .env"
    )


def _filter_unset_in_env(overrides: dict[str, Any]) -> dict[str, Any]:
    """Keep only override keys that are real fields AND not already set in `.env`.

    A key already in `.env` was the effective value anyway (env beat the
    override), so copying the dead override value would silently change config.
    """
    amap = _field_alias_map()
    present = set(read_env_aliases())
    return {
        name: value
        for name, value in overrides.items()
        if name in amap and amap[name] not in present
    }


def _legacy_host_json_path() -> Path:
    """The retired per-machine host override file."""
    return _ava_home() / "runtime_config.json"


def rename_env_keys(path: Path, renames: dict[str, str]) -> list[str]:
    """One-shot rename of legacy .env keys, returning a human line per change.

    A renamed config key leaves the OLD name in every .env written before the
    rename. Settings may keep reading the legacy name as a fallback, but the
    panel writes and preflights compare only the new name, so the legacy key
    left in place is a silent second source. Rewrite it here, once; a second
    run finds no legacy keys and is a no-op. When both names exist the new one
    is authoritative and the legacy line is dropped (mirrors the
    disabled-services marker rule). Returns one line per changed/dropped key.
    """
    if not path.exists():
        return []
    with file_lock(env_lock_path(path), timeout_s=ENV_LOCK_TIMEOUT_S):
        raw = dotenv_values(path)
        changed: list[str] = []
        lines = path.read_text().splitlines(keepends=True)
        out: list[str] = []
        for line in lines:
            key = line.split("=", 1)[0].strip()
            if key in renames:
                new_key = renames[key]
                if new_key in raw and raw[new_key] is not None:
                    # New key already present and set — the legacy line is stale.
                    changed.append(f"{key} dropped ({new_key} authoritative)")
                    continue
                out.append(f"{new_key}={line.split('=', 1)[1]}")
                changed.append(f"{key} -> {new_key}")
                continue
            out.append(line)
        if changed:
            path.write_text("".join(out))
    return changed


def migrate_permissions_helper_env_keys(env_path: Path) -> list[str]:
    """One-shot rename of the pre-rename AVA_NATIVE_HELPER_* keys in `env_path`.

    The desktop-automation daemon was renamed native-helper ->
    permissions-helper; clusters born before the rename carry the old keys.
    Settings still reads them as a fallback, so this is a hygiene migration to
    a single canonical name (see `rename_env_keys` for the both-present rule).
    """
    return rename_env_keys(
        env_path,
        {
            "AVA_NATIVE_HELPER_ENABLED": "AVA_PERMISSIONS_HELPER_ENABLED",
            "AVA_NATIVE_HELPER_PORT": "AVA_PERMISSIONS_HELPER_PORT",
        },
    )


# The inverted-semantics legacy keys (see shared/dotenv_boot for the boot-time
# translation): legacy key -> canonical key. The value must be INVERTED on the
# rename — the old "skip X" boolean means the opposite of the new "X enabled".
_SKIP_ALIAS_RENAMES = {
    "AVA_SKIP_AUTH": "AVA_AUTH_MIDDLEWARE_ENABLED",
    "AVA_SKIP_SECURITY_SCAN": "AVA_SECURITY_SCAN_ENABLED",
}
_SKIP_ALIAS_TRUE = frozenset({"1", "true", "yes", "on", "t", "y"})
_SKIP_ALIAS_FALSE = frozenset({"0", "false", "no", "off", "f", "n"})


def migrate_skip_alias_env_keys(env_path: Path) -> list[str]:
    """One-shot rename of the inverted-semantics legacy AVA_SKIP_* keys in
    `env_path`, inverting each value.

    The 2026-07-06 affirmative-naming refactor (#315) renamed
    skip_auth_middleware -> auth_middleware_enabled and skip_security_scan ->
    security_scan_enabled with the default INVERTED. Settings keeps reading the
    legacy keys (translated at boot), but the canonical name must win ON DISK:
    the config panel writes the canonical alias, and a legacy key left in the
    file is a silent second source that outranks it (AliasChoices resolves the
    legacy key first when the canonical AVA_-prefixed name is absent). Rewrite
    here once, inverting the value; a second run finds no legacy keys and is a
    no-op. When both names exist the new one is authoritative and the legacy
    line is dropped (mirrors `rename_env_keys`). An unparseable legacy value is
    left in place unchanged (fail fast at Settings build, never guessed at).
    """
    if not env_path.exists():
        return []
    raw = dotenv_values(env_path)
    changed: list[str] = []
    lines = env_path.read_text().splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key not in _SKIP_ALIAS_RENAMES:
            out.append(line)
            continue
        new_key = _SKIP_ALIAS_RENAMES[key]
        if new_key in raw and raw[new_key] is not None:
            # New key already present and set — the legacy line is stale.
            changed.append(f"{key} dropped ({new_key} authoritative)")
            continue
        value = line.split("=", 1)[1] if "=" in line else ""
        token = value.strip().lower()
        if token in _SKIP_ALIAS_TRUE:
            out.append(f"{new_key}=false")
            changed.append(f"{key}=true -> {new_key}=false")
        elif token in _SKIP_ALIAS_FALSE:
            out.append(f"{new_key}=true")
            changed.append(f"{key}=false -> {new_key}=true")
        else:
            # Unparseable — keep the line verbatim; Settings will fail fast.
            out.append(line)
    if changed:
        env_path.write_text("".join(out))
    return changed


def migrate_primary_gateway_url_key(env_path: Path) -> list[str]:
    """One-shot rename of the deprecated AVA_PRIMARY_GATEWAY_URL key in
    `env_path` to AVA_GATEWAY_URL (value unchanged — a pure rename).

    The old name was scheduled for removal 2026-07-01 (gateway.py) but the
    deadline passed with the alias still resolving; instead of breaking
    un-converged .env files, every unit's file is renamed here (converge), and
    the alias keeps resolving for a grace period with an updated deadline. A
    second run finds no legacy key and is a no-op; when both names exist the
    new one is authoritative and the legacy line is dropped (rename_env_keys
    rule).
    """
    return rename_env_keys(env_path, {"AVA_PRIMARY_GATEWAY_URL": "AVA_GATEWAY_URL"})


def migrate_host_json_to_env() -> None:
    """One-time: copy this machine's retired host override file into its `.env`.

    Runs on every host in the converge phase. Precedence-correct (skips keys
    already in `.env`) and idempotent (the source file is archived to
    `*.migrated` after the copy, so a re-run finds nothing).
    """
    src = _legacy_host_json_path()
    if not src.exists():
        return
    try:
        data = json.loads(src.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        _log.warning(
            "migrate_host_json_to_env: %s unreadable, leaving in place", src, exc_info=True
        )
        return
    if not isinstance(data, dict):
        _log.warning("migrate_host_json_to_env: %s root not a dict, leaving in place", src)
        return
    to_write = _filter_unset_in_env(cast("dict[str, Any]", data))
    for name, value in to_write.items():
        _ensure_migratable(name, value)
    if to_write:
        write_fields(to_write, set())
    archived = src.parent / (src.name + ".migrated")
    try:
        src.rename(archived)
    except OSError:
        _log.warning(
            "migrate_host_json_to_env: rename %s failed (left in place)", src, exc_info=True
        )
    _log.info(
        "migrate_host_json_to_env: copied %d host override(s) into %s",
        len(to_write),
        env_file_path(),
    )

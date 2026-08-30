"""Profile-independent config read path (Task #856 D5) + startup utilities.

Under per-process profiles the `shared.config` singleton only constructs its
profile's sub-models; a domain outside the profile raises AttributeError on
access (fail-fast). The config-SERVICE paths — bootstrap_config_values (the
gateway serves every BOOTSTRAP_FIELDS to agent-runners), current_field_values
(the 231-field config panel), flat_dump (the config-overlay snapshot) — must
still read EVERY field, so they resolve a missing domain through a fresh full
Settings instance. The .env FILE remains the primary source for the service
paths (read fresh via runtime_config.read_env_aliases); the full instance only
supplies the effective-value fallback for fields absent from the file, reading
the current os.environ exactly as the pre-profile singleton did.

Lives in its own module (not shared/config/__init__.py) for the repo's
line-budget discipline; it imports the config package lazily because
`shared.config` builds on top of this module's primitives — the same lazy
pattern as `shared/runtime_config.py`.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

__all__ = [
    "_all_domains_settings",
    "_service_field_value",
    "bootstrap_config_values",
    "current_field_values",
    "warn_deprecated_env_aliases",
]


@lru_cache(maxsize=1)
def _all_domains_settings() -> Any:
    """A profile-less full Settings instance for the config-service read paths.

    Constructed once per process, lazily, from the current os.environ at first
    use. Never consulted when the running process has no profile (the
    singleton already constructs every domain), so profile-less processes —
    tests, CLI maintenance verbs, bare checkouts — pay nothing.
    """
    from shared.config import Settings

    # profile=None forces full construction even when the environment
    # carries AVA_PROCESS_PROFILE (D5).
    return Settings(profile=None)


def _service_field_value(name: str) -> Any:
    """Effective value of leaf field `name` for the config-service read paths.

    `get_field` (the module singleton) is the fast path; when the running
    profile excludes the field's domain — the singleton raises AttributeError,
    fail-fast — resolve through `_all_domains_settings()` so the bootstrap
    payload / config panel stay complete (D5). Callers that must NOT silently
    cross a profile boundary keep using `get_field` directly.
    """
    from shared.config import _FIELDS, settings

    ref = _FIELDS[name]
    try:
        return getattr(getattr(settings, ref.domain), name)
    except AttributeError:
        return getattr(getattr(_all_domains_settings(), ref.domain), name)


_DATA_PLANE_URL_ALIASES = ("AVA_DB_URL", "AVA_REDIS_URL")


def _serve_reachable_data_plane_hosts(out: dict[str, str]) -> None:
    """Rewrite loopback hosts in served data-plane URLs to this gateway's
    reachable address, in place.

    The cluster's own `.env` (and therefore the verbatim bootstrap payload) uses
    `127.0.0.1` in its db/redis URLs: the gateway dials itself over loopback and
    the data plane binds loopback first (`_dial_self_host_via_loopback` /
    `_bind_addrs`). A REMOTE agent-runner materializing that payload would dial
    ITS OWN loopback and hit itself — cross-machine enroll only works because
    the reachable host (`AVA_MACHINE_HOST` / `$AVA_HOME/machine_host`) is
    substituted here. A single box (reachable host = localhost) and an
    already-reachable URL host pass through unchanged; only the host is swapped
    — scheme / userinfo / port / database / query survive verbatim.
    """
    # Resolved through the config module (not data_plane directly) so tests
    # can monkeypatch shared.config._self_machine_host, as they always have.
    from shared.config import _self_machine_host
    from shared.netutil import is_loopback_host
    from shared.url_secret import url_with_host

    reachable = _self_machine_host()
    if is_loopback_host(reachable):
        return
    for alias in _DATA_PLANE_URL_ALIASES:
        value = out.get(alias)
        if not value:
            continue
        host = urlsplit(value).hostname or ""
        if host and is_loopback_host(host):
            out[alias] = url_with_host(value, reachable)


def warn_deprecated_env_aliases() -> None:
    """Emit a deprecation warning when a legacy env var alias is the active source.

    AVA_PRIMARY_GATEWAY_URL was renamed AVA_GATEWAY_URL (scheduled for removal
    2026-09-01 — the original 2026-07-01 deadline lapsed; converge renames the
    key in every unit's .env, see migrate_primary_gateway_url_key);
    AVA_SKIP_AUTH / AVA_SKIP_SECURITY_SCAN were renamed
    AVA_AUTH_MIDDLEWARE_ENABLED / AVA_SECURITY_SCAN_ENABLED with the meaning
    INVERTED (a value of "true" used to mean "skip", so it now means
    "disabled"). The old names still resolve via AliasChoices — boot-time
    translation (dotenv_boot) and the converge .env migration keep them
    correct — but each warns here so operators rename before the drop-day. Call
    once at process startup (gateway lifespan) so operators see the nudge while
    the alias still works, rather than discovering it broke on the drop-day.
    Logger import is deferred: shared.log imports settings from this package, so a
    top-level import would be circular.
    """
    from shared.log import logger

    if "AVA_PRIMARY_GATEWAY_URL" in os.environ and "AVA_GATEWAY_URL" not in os.environ:
        logger.warning(
            "AVA_PRIMARY_GATEWAY_URL is deprecated and scheduled for removal "
            "2026-09-01; rename it to AVA_GATEWAY_URL in your .env (a converge "
            "migration does this automatically)."
        )
    for legacy, canonical in (
        ("AVA_SKIP_AUTH", "AVA_AUTH_MIDDLEWARE_ENABLED"),
        ("AVA_SKIP_SECURITY_SCAN", "AVA_SECURITY_SCAN_ENABLED"),
    ):
        if legacy in os.environ and canonical not in os.environ:
            logger.warning(
                f"{legacy} is deprecated with INVERTED semantics — {legacy}=true now "
                f"means the renamed {canonical}=false. Rename it to {canonical} "
                f"in your .env (a converge migration does this automatically)."
            )


@lru_cache(maxsize=1)
def _domain_model_classes() -> dict[str, type[Any]]:
    """`{domain attr: sub-model class}` — the registry's deferred class imports,
    resolved once per process without constructing any Settings."""
    from importlib import import_module

    from shared.config_registry import _DOMAIN_MODELS, _MODEL_CLASSES

    out: dict[str, type[Any]] = {}
    for attr, _label, model_name, _capability in _DOMAIN_MODELS:
        if isinstance(model_name, str):
            out[attr] = getattr(import_module(_MODEL_CLASSES[model_name]), model_name)
        else:
            out[attr] = model_name
    return out


def current_field_values() -> dict[str, Any]:
    """Map every field name to its current value.

    For a field whose alias is set in this unit's `.env` FILE, the fresh file
    value is used — decoded through the field's OWN sub-model (one
    `model_validate` per domain), so the model's field validators run exactly as
    they do at Settings construction: a NoDecode comma-list ("weixin,feishu")
    splits into the typed list instead of failing a bare
    `TypeAdapter(list[str])`. For a field absent from the file, the boot-time
    value is used (the effective env value, which already folds in os.environ +
    the Field default).

    The config panel and the bootstrap payload both read through this, so an edit
    takes effect on the next consuming-process restart while `restart_required`
    signals which process that is.
    """

    from shared import runtime_config
    from shared.config import _FIELDS, field_alias

    aliases = runtime_config.read_env_aliases()
    out: dict[str, Any] = {}
    # Group the file-present fields by owning domain: one model_validate per
    # domain decodes them all through the sub-model's own validators.
    pending: dict[str, list[tuple[str, str, str]]] = {}
    for name, ref in _FIELDS.items():
        alias = field_alias(name)
        if alias not in aliases:
            out[name] = _service_field_value(name)
            continue
        pending.setdefault(ref.domain, []).append((name, alias, aliases[alias]))
    for domain, batch in pending.items():
        _decode_env_file_values(domain, batch, out)
    return out


def _isolated_domain_payload(model: type[Any], file_overrides: dict[str, str]) -> dict[str, Any]:
    """A full-field payload that makes ``model_validate`` env-independent.

    Every field of the sub-model carries its boot-time value; the ``file_overrides``
    fields then override with their raw `.env` strings. Because EVERY field is
    provided (keyed by field name — EnvSettings has populate_by_name=True),
    pydantic-settings has nothing left to read from os.environ: a bad env value
    for a field absent from the file can no longer poison the decode of a good
    file value (QA #1090 repro — the batch and the per-field retry used to read
    env for non-file fields, and one bad env value made a good file value get
    dropped).
    """
    payload = {name: _service_field_value(name) for name in model.model_fields}
    payload.update(file_overrides)
    return payload


def _decode_env_file_values(
    domain: str, batch: list[tuple[str, str, str]], out: dict[str, Any]
) -> None:
    """Decode one domain's raw `.env` values through its owning sub-model.

    The previous path validated the raw string against a bare
    `TypeAdapter(annotation)` — which cannot see the model's field validators
    (NoDecode metadata and the before-validators live on the class, not the
    annotation) — so every NoDecode comma-list field warned "cannot be decoded"
    on each panel read / agent spawn and fell back to a wrong-typed comma split
    (a `list[float]` field came back `list[str]`). Validating through the model
    reuses its validators and coercion, so comma lists and JSON arrays decode
    silently and with the right types.

    On a batch failure (a genuinely bad FILE value), each field is re-validated
    alone — with the OTHER file fields reverted to their boot values so one bad
    line hides nothing else: good values still land, the bad one warns and falls
    back to the boot-time value.
    """
    model = _domain_model_classes()[domain]
    file_values = {name: raw for name, _alias, raw in batch}
    try:
        decoded = model.model_validate(_isolated_domain_payload(model, file_values))
    except ValidationError:
        for name, alias, raw in batch:
            try:
                out[name] = getattr(
                    model.model_validate(_isolated_domain_payload(model, {name: raw})),
                    name,
                )
            except ValidationError:
                _warn_undecodable_field(name, alias, raw)
                out[name] = _service_field_value(name)
        return
    for name, _alias, _raw in batch:
        out[name] = getattr(decoded, name)


def _warn_undecodable_field(name: str, alias: str, raw: str) -> None:
    """Warn about a `.env` value the owning model cannot decode — the next
    process start's Settings construction will fail on it, so the operator must
    hear about it at panel-read time rather than at the next boot (audit
    round-2 config.md P2)."""
    from shared.log import logger

    logger.warning(
        f"current_field_values: {alias}={raw!r} in .env cannot be decoded "
        f"by the {name!r} config field; serving the boot-time value instead — "
        f"fix or remove the line before the next process start (Settings "
        f"construction will fail on it)"
    )


def bootstrap_config_values(role: str | None = None) -> dict[str, str]:
    """Return {ENV_ALIAS: value} for the BOOTSTRAP_FIELDS that are set.

    Values are unmasked (the caller is an authenticated machine that needs the
    real connection strings + secrets). A field set in the gateway's `.env` is
    served as its raw `.env` text verbatim (already the env-string form the
    recipient re-parses, including a comma-list), read fresh — so a rotated cluster
    secret reaches an agent on its next restart without the gateway itself
    restarting. One deliberate exception: the data-plane URL aliases
    (`AVA_DB_URL` / `AVA_REDIS_URL`) have their loopback host rewritten to this
    gateway's reachable address (`_serve_reachable_data_plane_hosts`) — required
    for cross-machine enroll, everything else survives verbatim. A field absent
    from `.env` is served as its stringified boot-time value; only None is skipped
    (env can't express "no value"), so the recipient falls back to the field
    default. An empty string IS served: it is the env form of an explicit
    set-to-empty (e.g. AVA_SKILLS_TO_INJECT_INTO_SYSTEM_PROMPT="" on a bench
    gateway), and dropping it would silently revert the recipient to the field
    default — exactly the distinction between "unset" and "set to empty".

    `role` selects the credential projection: `None` and `"runner"` both rewrite the served
    `AVA_DB_URL` to the least-privilege `ava_runner` role with its own password
    (the gateway .env AVA_RUNNER_DB_PASSWORD — carried INSIDE the URL, never
    served as a standalone key), so every bootstrap recipient dials exactly the
    surface its role grants and nothing more (Task #1236). A request on a cluster
    that has no runner credential yet raises: serving an empty password would fail
    at first connect with an unexplained auth error, so the operator is told to
    provision the role instead.
    """
    from pydantic import SecretStr

    from shared import runtime_config
    from shared.config import BOOTSTRAP_FIELDS, field_alias
    from shared.url_secret import url_with_userinfo

    if role not in (None, "runner"):
        raise ValueError(
            f"bootstrap role {role!r} is not a known projection — supported: "
            f"'runner' or None (the least-privilege ava_runner URL)"
        )
    aliases = runtime_config.read_env_aliases()
    out: dict[str, str] = {}
    for name in BOOTSTRAP_FIELDS:
        alias = field_alias(name)
        if alias in aliases:
            out[alias] = aliases[alias]
            continue
        value = _service_field_value(name)
        if value is None:
            continue
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        out[alias] = runtime_config.env_value_text(value)
    _serve_reachable_data_plane_hosts(out)
    # Provider keys are not Settings fields, so they cannot arrive through
    # BOOTSTRAP_FIELDS. Read only declared keys from the raw gateway .env; this
    # is the authenticated, fresh-file channel a split runner materializes.
    from shared.lm import provider_api
    from shared.lm._plugin_providers import ensure_provider_plugins_loaded

    ensure_provider_plugins_loaded()
    for binding in provider_api.REGISTRY.bindings.values():
        if binding.key_env in aliases and binding.key_env not in out:
            out[binding.key_env] = aliases[binding.key_env]
    from shared.cluster.derive import RUNNER_DB_PASSWORD_ENV, RUNNER_ROLE

    runner_password = aliases.get(RUNNER_DB_PASSWORD_ENV) or ""
    if not runner_password:
        if _is_remote_data_plane(out):
            raise ValueError(
                "AVA_RUNNER_DB_PASSWORD is not set in the gateway's .env — on a "
                "remote-managed data plane the runner role is provisioned at the "
                "provider and its password must be written into the gateway .env "
                "directly (`ava cluster ensure-db-role` refuses remote planes)."
            )
        raise ValueError(
            "AVA_RUNNER_DB_PASSWORD is not set in the gateway's .env — run "
            "`ava cluster ensure-db-role` on the gateway first, then retry."
        )
    db_url = out.get("AVA_DB_URL")
    if not db_url:
        raise ValueError(
            "AVA_DB_URL is not served by bootstrap — cannot project the runner credential onto it"
        )
    out["AVA_DB_URL"] = url_with_userinfo(db_url, RUNNER_ROLE, runner_password)
    return out


def _is_remote_data_plane(env: dict[str, str]) -> bool:
    """Whether the served env's data-plane URLs name a foreign host — the
    bootstrap payload is the raw `.env` text, so the remote predicate is read
    from it directly (mirrors `settings.data_plane.is_remote` on the serving
    side)."""
    from shared.netutil import is_loopback_host
    from shared.url_secret import url_host

    for key in ("AVA_DB_URL", "AVA_REDIS_URL"):
        url = (env.get(key) or "").strip()
        if url and url_host(url) and not is_loopback_host(url_host(url)):
            return True
    return False

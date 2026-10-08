"""Profile-independent config read path (Task #856 D5) + startup utilities.

Under per-process profiles the `base.config` singleton only constructs its
profile's sub-models; a domain outside the profile raises AttributeError on
access (fail-fast). The config-SERVICE paths — bootstrap_config_values (the
gateway serves every BOOTSTRAP_FIELDS to agent-runners), current_field_values
(the 231-field config panel), flat_dump (the config-overlay snapshot) — must
still read EVERY field, so they resolve a missing domain through a fresh full
Settings instance. The .env FILE remains the primary source for the service
paths (read fresh via runtime_config.read_env_aliases); the full instance only
supplies the effective-value fallback for fields absent from the file, reading
the current os.environ exactly as the pre-profile singleton did.

Lives in its own module (not base/config/__init__.py) for the repo's
line-budget discipline; it imports the config package lazily because
`base.config` builds on top of this module's primitives — the same lazy
pattern as `base/host/env/runtime_config.py`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

__all__ = [
    "_all_domains_settings",
    "_service_field_value",
    "bootstrap_config_values",
    "current_field_values",
    "domain_model_classes",
]


@lru_cache(maxsize=1)
def _all_domains_settings() -> Any:
    """A profile-less full Settings instance for the config-service read paths.

    Constructed once per process, lazily, from the current os.environ at first
    use. Never consulted when the running process has no profile (the
    singleton already constructs every domain), so profile-less processes —
    tests, CLI maintenance verbs, bare checkouts — pay nothing.
    """
    from base.config import Settings

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
    from base.config import _FIELDS, settings

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
    ITS OWN loopback and hit itself — a remote runner's join only works because
    the reachable host (`AVA_MACHINE_HOST`) is
    substituted here. A single box (reachable host = localhost) and an
    already-reachable URL host pass through unchanged; only the host is swapped
    — scheme / userinfo / port / database / query survive verbatim.
    """
    # Resolved through the config module (not data_plane directly) so tests
    # can monkeypatch base.config._self_machine_host, as they always have.
    from base.config import _self_machine_host
    from base.host.net.predicates import is_loopback_host
    from base.host.net.url_secret import url_with_host

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


@lru_cache(maxsize=1)
def domain_model_classes() -> dict[str, type[Any]]:
    """`{domain attr: sub-model class}` — the registry's deferred class imports,
    resolved once per process without constructing any Settings. Public because
    overlay validation (`base.packages.plugins.config_registration`) re-validates each
    framework field against its owning sub-model class."""
    from importlib import import_module

    from base.host.env.config_registry import DOMAIN_MODELS, MODEL_CLASSES

    out: dict[str, type[Any]] = {}
    for attr, _label, model_name, _capability in DOMAIN_MODELS:
        if isinstance(model_name, str):
            out[attr] = getattr(import_module(MODEL_CLASSES[model_name]), model_name)
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

    from base.config import _FIELDS, field_alias
    from base.host.env import runtime_config

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

    On a batch failure, each field is re-validated alone — with the OTHER file
    fields reverted to their boot values so one bad line hides nothing else:
    good values still land, the bad one warns and falls back to the boot-time
    value.

    One failure is not a bad value: an agent-profile process at the default
    home decoding the owner `AVA_DB_URL` line hits the deliberate guard refusal
    (`AgentProfileOwnerDbUrlRefusedError`, #4332). That topology is expected — the
    boot-time value is the launcher-injected runner projection — so it is
    served SILENTLY (debug-logged only): warning on every fresh read (every
    panel read / agent send) would bury the genuine decode failures.
    """
    model = domain_model_classes()[domain]
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
            except ValidationError as exc:
                if _is_expected_owner_url_refusal(exc):
                    from base.log import logger

                    logger.debug(
                        f"current_field_values: serving the boot-time {alias} "
                        f"(expected agent-profile owner-URL refusal)"
                    )
                else:
                    _warn_undecodable_field(name, alias, raw)
                out[name] = _service_field_value(name)
        return
    for name, _alias, _raw in batch:
        out[name] = getattr(decoded, name)


def _is_expected_owner_url_refusal(exc: ValidationError) -> bool:
    """Whether `exc` is only the agent-profile owner-URL guard refusing (#4332).

    The guard raises `AgentProfileOwnerDbUrlRefusedError` and pydantic keeps the
    original exception instance in `err["ctx"]["error"]`, so this is a type
    test — never a message match. EVERY reported error must be the refusal:
    anything else is a genuine decode failure and warns.
    """
    from base.config.domains.storage.data_plane import AgentProfileOwnerDbUrlRefusedError

    errors = exc.errors()
    return bool(errors) and all(
        isinstance((item.get("ctx") or {}).get("error"), AgentProfileOwnerDbUrlRefusedError)
        for item in errors
    )


def _warn_undecodable_field(name: str, alias: str, _raw: str) -> None:
    """Warn about a `.env` value the owning model cannot decode — the next
    process start's Settings construction will fail on it, so the operator must
    hear about it at panel-read time rather than at the next boot (audit
    round-2 config.md P2).

    Only genuinely undecodable values reach this: the expected agent-profile
    owner-URL refusal is classified out by `_is_expected_owner_url_refusal`.
    """
    from base.log import logger

    logger.warning(
        f"current_field_values: {alias} in .env cannot be decoded "
        f"by the {name!r} config field; serving the boot-time value instead — "
        f"fix the line before the next process start (Settings "
        f"construction will fail on it)"
    )


def bootstrap_config_values() -> dict[str, str]:
    """Return {ENV_ALIAS: value} for the BOOTSTRAP_FIELDS that are set.

    Values are unmasked (the caller is an authenticated machine). A field set in
    the gateway's `.env` is served as its raw `.env` text verbatim (already the
    env-string form the recipient re-parses, including a comma-list), read fresh
    — so a `.env` edit reaches a recipient on its next restart without the
    gateway itself restarting. The data-plane URL aliases
    (`AVA_DB_URL` / `AVA_REDIS_URL`) have their loopback host rewritten to this
    gateway's reachable address (`_serve_reachable_data_plane_hosts`) — required
    for a remote unit on another machine. AVA_GATEWAY_OTLP_ENDPOINT is derived from this
    gateway's reachable host and OTLP port; local receiver settings are not
    distributed. A field absent from `.env` is served as its stringified
    boot-time value, except the required DB URL, which must come from this fresh
    snapshot. Only None is skipped (env can't express "no value"), so the
    recipient falls back to the field default. An empty string IS served: it is
    the env form of an explicit set-to-empty (e.g.
    AVA_SKILLS_TO_INJECT_INTO_SYSTEM_PROMPT="" on a bench gateway), and dropping
    it would silently revert the recipient to the field default — exactly the
    distinction between "unset" and "set to empty".

    `AVA_DB_URL` is served as the CREDENTIAL-FREE endpoint (`served_db_endpoint`):
    bootstrap hands out configuration, never a database login. A remote
    agent-runner receives its runner login only in the capability bundle the
    gateway operator issues for that unit (`base.cluster.authority.unit`; the
    login is the write generation's, shared by every runner unit), so a stale
    runner holding the bearer cannot reacquire the current write generation
    here.
    """
    from pydantic import SecretStr

    from base.config import BOOTSTRAP_FIELDS, field_alias
    from base.host.env import runtime_config

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
    out["AVA_DB_URL"] = served_db_endpoint(aliases)
    out["AVA_GATEWAY_OTLP_ENDPOINT"] = _gateway_otlp_projection(aliases)
    # Provider keys are not Settings fields, so they cannot arrive through
    # BOOTSTRAP_FIELDS. Read only declared keys from the raw gateway .env; this
    # is the authenticated, fresh-file channel a split runner materializes.
    from base.lm.plugin_providers import model_catalog

    for binding in model_catalog().bindings.values():
        if binding.key_env in aliases and binding.key_env not in out:
            out[binding.key_env] = aliases[binding.key_env]
    from base.host.env.registry import PLUGIN_CLUSTER_CONFIG_ENV

    out[PLUGIN_CLUSTER_CONFIG_ENV] = plugin_bootstrap_config()
    return out


def plugin_bootstrap_config() -> str:
    """Serialize only declared, non-secret cluster policy from plugin authority images."""
    import json

    from base.config import schema_extra
    from base.packages.plugins.config_face import declared_config_class
    from base.packages.plugins.config_registration import disk_image_path, read_authority_config
    from base.packages.plugins.enable_config import discover_plugins

    payload: dict[str, dict[str, object]] = {}
    for plugin, plugin_dir in sorted(discover_plugins().items()):
        cls = declared_config_class(plugin, plugin_dir)
        if cls is None:
            continue
        fields: list[str] = []
        for name, info in cls.model_fields.items():
            extra = schema_extra(info)
            if extra.get("scope") not in {
                "cluster-pinned",
                "cluster-default",
            }:
                continue
            if extra.get("sensitive"):
                raise ValueError(f"plugin cluster config {plugin}.{name} may not carry secrets")
            fields.append(name)
        if fields:
            config = read_authority_config(plugin, cls, disk_image_path(plugin))
            values = config.model_dump(mode="json")
            payload[plugin] = {name: values[name] for name in fields}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def served_db_endpoint(aliases: dict[str, str] | None = None) -> str:
    """The database endpoint this gateway serves to agent-runners: its `.env`
    `AVA_DB_URL` without any password, loopback rewritten to the reachable host.

    A local plane's `.env` already holds the credential-free endpoint; a
    remote-managed plane's provider URL loses its password here. Raises when
    the gateway's fresh config snapshot has no `AVA_DB_URL`."""
    from base.cluster.authority.unit import credential_free
    from base.host.env import runtime_config

    if aliases is None:
        aliases = runtime_config.read_env_aliases()
    db_url = aliases.get("AVA_DB_URL")
    if not db_url:
        raise ValueError("AVA_DB_URL is missing from the gateway config snapshot")
    served = {"AVA_DB_URL": credential_free(db_url)}
    _serve_reachable_data_plane_hosts(served)
    return served["AVA_DB_URL"]


def _gateway_otlp_projection(aliases: dict[str, str]) -> str:
    """Publish this gateway's ingress without distributing its local listener settings."""
    from base.config import _self_machine_host
    from base.host.net.url_secret import url_with_host

    port = int(
        aliases.get("AVA_TELEMETRY_OTLP_PORT", str(_service_field_value("telemetry_otlp_port")))
    )
    if not 1 <= port <= 65535:
        raise ValueError("AVA_TELEMETRY_OTLP_PORT must be between 1 and 65535")
    host = aliases.get("AVA_MACHINE_HOST") or _self_machine_host()
    return url_with_host(f"http://localhost:{port}", host)

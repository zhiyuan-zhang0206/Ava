"""Explicit owner of boot read models and fresh unit configuration reads.

Composition roots supply both the process-profile runtime and a complete
read model. File reads use the owner's fixed path, so two roots do not share
configuration or follow a later change to AVA_HOME. Runtime profile access
remains restricted; only this service's complete read surface crosses domains.
Invalid file values propagate their owning model's validation error. Repair
commands use class metadata and candidate validation without constructing an
authority or validating unrelated configuration.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import dotenv_values

from base.host.env.config_registry import DOMAIN_MODELS, field_alias, fields, schema_extra

__all__ = [
    "ConfigAuthority",
    "bootstrap_config_values",
    "current_field_values",
    "domain_model_classes",
    "served_db_endpoint",
]


def _validate_complete_model(model: Any) -> None:
    if model.profile is not None:
        raise ValueError("ConfigAuthority requires a profile-independent read model")


class _DeferredReadModel:
    """Memoize a supplied complete model only after successful construction."""

    profile = None

    def __init__(self, build: Callable[[], Any]) -> None:
        self._build: Callable[[], Any] | None = build
        self._model: Any = None
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        with self._lock:
            if self._build is not None:
                model = self._build()
                _validate_complete_model(model)
                self._model = model
                self._build = None
            return getattr(self._model, name)


@dataclass(frozen=True, slots=True)
class ConfigAuthority:
    """One root's explicit runtime, complete read model and unit config path.

    ``all_domains`` must be built with ``profile=None`` by the composition
    root. It may be the runtime itself when that runtime is already complete.
    Missing file fields retain this owner's validated model values. Fresh reads
    do not mutate either model and are never memoized.
    """

    runtime: Any
    all_domains: Any
    env_path: Path

    def __post_init__(self) -> None:
        _validate_complete_model(self.all_domains)
        if not self.env_path.is_absolute():
            raise ValueError("ConfigAuthority env_path must be absolute")

    @classmethod
    def deferred(
        cls, *, runtime: Any, build_all_domains: Callable[[], Any], env_path: Path
    ) -> ConfigAuthority:
        """Retain a root's complete-model factory without forcing a lite process to upgrade.

        The first read outside the supplied runtime constructs and validates the
        complete model from the environment at that first use. Successful
        construction is memoized; failures propagate without an internal retry.
        Fresh file reads remain uncached and use this authority's fixed path.
        """
        return cls(runtime, _DeferredReadModel(build_all_domains), env_path)

    def read_env_aliases(self) -> dict[str, str]:
        """Read this authority's file once; preserve explicitly empty values."""
        if not self.env_path.exists():
            return {}
        return {
            name: value for name, value in dotenv_values(self.env_path).items() if value is not None
        }

    def service_field_value(self, name: str) -> Any:
        """Read an owned model value without recovering arbitrary AttributeError failures."""
        domain = fields()[name].domain
        source = self.runtime if self.runtime.has_domain(domain) else self.all_domains
        return getattr(getattr(source, domain), name)

    def flat_dump(self, mode: str = "python") -> dict[str, Any]:
        """The complete boot snapshot used when binding an agent overlay."""
        out: dict[str, Any] = {}
        for domain, *_rest in DOMAIN_MODELS:
            source = self.runtime if self.runtime.has_domain(domain) else self.all_domains
            out.update(getattr(source, domain).model_dump(mode=mode))
        return out

    def current_field_values(self) -> dict[str, Any]:
        """Decode fresh file fields in complete domain batches, failing on invalid values.

        Once the complete read model is constructed, providing every domain
        field from the owned models prevents later ambient environment changes
        from supplying or poisoning values.
        Domain validation keeps field coercion and cross-field invariants.
        """
        aliases = self.read_env_aliases()
        out: dict[str, Any] = {}
        pending: dict[str, dict[str, str]] = {}
        for name, ref in fields().items():
            alias = field_alias(name)
            if alias in aliases:
                pending.setdefault(ref.domain, {})[name] = aliases[alias]
            else:
                out[name] = self.service_field_value(name)
        for domain, file_values in pending.items():
            model = domain_model_classes()[domain]
            payload = {name: self.service_field_value(name) for name in model.model_fields}
            payload.update(file_values)
            if domain == "data_plane":
                self._project_runner_db_url(payload)
            decoded = model.model_validate(payload)
            out.update({name: getattr(decoded, name) for name in file_values})
        return out

    def _project_runner_db_url(self, payload: dict[str, Any]) -> None:
        """Choose the delivered runner login before validating the owner file.

        A local owner URL at the default home is an endpoint for launchers,
        not the agent's login. Only that existing guarded topology uses the
        runtime projection. Malformed URLs continue into normal validation;
        invalid fields never recover from a validation failure.
        """
        from base.config.domains.storage.data_plane import project_service_db_url

        raw = payload["db_url"]
        payload["db_url"] = project_service_db_url(
            payload["db_url"],
            self.runtime.data_plane.db_url,
            profile=self.runtime.profile,
            home=self.env_path.parent,
            cluster_secret=payload["cluster_secret"],
            machine_host=self.runtime.general.machine_host,
        )
        if payload["db_url"] != raw:
            from base.log import logger

            logger.debug("current_field_values: serving the delivered runner AVA_DB_URL")

    def bootstrap_config_values(
        self, *, provider_key_envs: Iterable[str], plugin_cluster_config: str
    ) -> dict[str, str]:
        """Fresh authenticated bootstrap packet, with no database credential.

        Raw file values retain their env syntax for recipient-side validation.
        Provider secrets arrive only through the catalog's declared key names;
        plugin policy is supplied by the plugin authority's fresh packet.
        """
        from pydantic import SecretStr

        from base.host.env import runtime_config
        from base.host.env.registry import PLUGIN_CLUSTER_CONFIG_ENV

        aliases = self.read_env_aliases()
        out: dict[str, str] = {}
        for name, ref in fields().items():
            extra = schema_extra(ref.info)
            if extra.get("scope") not in ("cluster-pinned", "cluster-default"):
                continue
            if extra.get("bootstrap", True) is False:
                continue
            alias = field_alias(name)
            if alias in aliases:
                out[alias] = aliases[alias]
                continue
            value = self.service_field_value(name)
            if value is None:
                continue
            if isinstance(value, SecretStr):
                value = value.get_secret_value()
            out[alias] = runtime_config.env_value_text(value)
        _serve_reachable_data_plane_hosts(out, self._reachable_host())
        out["AVA_DB_URL"] = self.served_db_endpoint(aliases)
        out["AVA_GATEWAY_OTLP_ENDPOINT"] = self._gateway_otlp_projection(aliases)
        for key_env in provider_key_envs:
            if key_env in aliases and key_env not in out:
                out[key_env] = aliases[key_env]
        out[PLUGIN_CLUSTER_CONFIG_ENV] = plugin_cluster_config
        return out

    def served_db_endpoint(self, aliases: dict[str, str] | None = None) -> str:
        """Read this gateway's required fresh endpoint and strip its password."""
        from base.cluster.authority.unit import credential_free

        if aliases is None:
            aliases = self.read_env_aliases()
        db_url = aliases.get("AVA_DB_URL")
        if not db_url:
            raise ValueError("AVA_DB_URL is missing from the gateway config snapshot")
        served = {"AVA_DB_URL": credential_free(db_url)}
        _serve_reachable_data_plane_hosts(served, self._reachable_host())
        return served["AVA_DB_URL"]

    def _reachable_host(self) -> str:
        return self.service_field_value("machine_host").strip() or "localhost"

    def _gateway_otlp_projection(self, aliases: dict[str, str]) -> str:
        """Project gateway ingress without distributing local listener settings."""
        from base.host.net.url_secret import url_with_host

        port = int(
            aliases.get(
                "AVA_TELEMETRY_OTLP_PORT", str(self.service_field_value("telemetry_otlp_port"))
            )
        )
        if not 1 <= port <= 65535:
            raise ValueError("AVA_TELEMETRY_OTLP_PORT must be between 1 and 65535")
        host = aliases.get("AVA_MACHINE_HOST") or self._reachable_host()
        return url_with_host(f"http://localhost:{port}", host)


def _serve_reachable_data_plane_hosts(out: dict[str, str], reachable: str) -> None:
    """Rewrite served loopback endpoints to the root's reachable address."""
    from base.host.net.predicates import is_loopback_host
    from base.host.net.url_secret import url_with_host

    if is_loopback_host(reachable):
        return
    for alias in ("AVA_DB_URL", "AVA_REDIS_URL"):
        value = out.get(alias)
        if not value:
            continue
        host = urlsplit(value).hostname or ""
        if host and is_loopback_host(host):
            out[alias] = url_with_host(value, reachable)


@lru_cache(maxsize=1)
def domain_model_classes() -> dict[str, type[Any]]:
    """Resolve registry class metadata without constructing any Settings."""
    from base.host.env.config_registry import model_class

    return {
        attr: model_class(model) if isinstance(model, str) else model
        for attr, _label, model, _capability in DOMAIN_MODELS
    }


def current_field_values(authority: ConfigAuthority) -> dict[str, Any]:
    """Read fresh values through the caller's explicit authority."""
    return authority.current_field_values()


def bootstrap_config_values(
    authority: ConfigAuthority, *, provider_key_envs: Iterable[str], plugin_cluster_config: str
) -> dict[str, str]:
    """Build a packet using the caller's authority and declared provider keys."""
    return authority.bootstrap_config_values(
        provider_key_envs=provider_key_envs, plugin_cluster_config=plugin_cluster_config
    )


def served_db_endpoint(authority: ConfigAuthority, aliases: dict[str, str] | None = None) -> str:
    """Read the caller's credential-free gateway endpoint."""
    return authority.served_db_endpoint(aliases)


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

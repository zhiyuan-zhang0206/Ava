"""Build the `WebDeps` of this process from its SDK installation: configuration, providers, ports."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from ava.sdk_surface import settings as sdk_settings
from ava.sdk_surface.injection_check import begin_check
from ava.security import finding_delivery_active, record_model_finding, scan_content
from ava.web.deps import BraveConfig, FetchModel, JinaConfig, WebDeps
from base.config.service_read import ConfigAuthority
from base.host.net.ports import SYSTEM_URL_OPENER
from base.lm import factory

__all__ = ["web_deps", "web_deps_for"]


def web_deps() -> WebDeps:
    """The deps for one web call, read from the configuration owner of the SDK installation."""
    return web_deps_for(sdk_settings.config_authority())


def web_deps_for(authority: ConfigAuthority) -> WebDeps:
    """The deps for one web call, read from `authority`."""
    return WebDeps(
        brave=BraveConfig(
            api_key=authority.service_field_value("brave_api_key"),
            endpoint=authority.service_field_value("web_brave_search_endpoint"),
            timeout_s=authority.service_field_value("web_search_timeout_seconds"),
            max_count=authority.service_field_value("web_max_search_results"),
        ),
        jina=JinaConfig(
            api_key=authority.service_field_value("jina_api_key"),
            reader_base=authority.service_field_value("web_jina_reader_base"),
            timeout_s=authority.service_field_value("web_fetch_timeout_seconds"),
        ),
        fetch_model=FetchModel(
            model=authority.service_field_value("web_fetch_model"),
            reasoning=authority.service_field_value("web_fetch_reasoning"),
            retry_attempts=authority.service_field_value("llm_invoke_retry_attempts"),
            retry_delay_seconds=authority.service_field_value("llm_invoke_retry_delay_seconds"),
            retry_max_delay_seconds=authority.service_field_value(
                "llm_invoke_retry_max_delay_seconds"
            ),
            timeout_seconds=authority.service_field_value("llm_invoke_timeout_seconds"),
            llm_override=authority.service_field_value("llm_override"),
            catalog=sdk_settings.model_catalog(),
            overrides=sdk_settings.model_overrides(),
        ),
        open_url=SYSTEM_URL_OPENER,
        build_chat_model=factory.build_chat_model,
        scan=_scan,
        begin_check=_checker(),
    )


def _scan(content: str, source: str) -> str:
    return scan_content(content, source=source)


def _checker() -> Callable[[str, str, str], Callable[[], None]]:
    return partial(
        begin_check, delivery_active=finding_delivery_active, record=record_model_finding
    )

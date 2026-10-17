from __future__ import annotations

from collections.abc import Callable
from functools import partial

from ava.sdk_surface import settings as sdk_settings
from ava.sdk_surface.injection_check import begin_check
from ava.security import finding_delivery_active, record_model_finding, scan_content
from ava.web.deps import BraveConfig, FetchDeps, FetchModel, JinaConfig, SearchDeps
from base.config.service_read import ConfigAuthority
from base.host.env.agent_slices import ModelOverrides
from base.host.net.ports import SYSTEM_URL_OPENER
from base.lm import factory
from base.lm.catalog import ModelCatalog

__all__ = ["fetch_deps", "fetch_deps_for", "search_deps", "search_deps_for"]


def search_deps() -> SearchDeps:
    return search_deps_for(sdk_settings.config_authority())


def search_deps_for(authority: ConfigAuthority) -> SearchDeps:
    return SearchDeps(
        brave=BraveConfig(
            api_key=authority.service_field_value("brave_api_key"),
            endpoint=authority.service_field_value("web_brave_search_endpoint"),
            timeout_s=authority.service_field_value("web_search_timeout_seconds"),
            max_count=authority.service_field_value("web_max_search_results"),
        ),
        open_url=SYSTEM_URL_OPENER,
        scan=_scan,
    )


def fetch_deps() -> FetchDeps:
    return fetch_deps_for(
        sdk_settings.config_authority(),
        catalog=sdk_settings.model_catalog(),
        overrides=sdk_settings.model_overrides(),
    )


def fetch_deps_for(
    authority: ConfigAuthority,
    *,
    catalog: ModelCatalog,
    overrides: ModelOverrides,
) -> FetchDeps:
    return FetchDeps(
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
            catalog=catalog,
            overrides=overrides,
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

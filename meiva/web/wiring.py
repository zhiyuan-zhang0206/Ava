# meiva:ref meiva/web/docs/web.meiva.okf.md#webdeps-fetch-model-boundary

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import cast

from base.config.service_read import ConfigAuthority
from base.host.env.agent_slices import ModelOverrides
from base.host.net.ports import SYSTEM_URL_OPENER
from base.lm import factory
from base.lm.catalog import ModelCatalog
from meiva.sdk_surface import settings as sdk_settings
from meiva.sdk_surface.injection_check import begin_check
from meiva.security import finding_delivery_active, record_model_finding, scan_content
from meiva.web.deepseek import RateLimiter
from meiva.web.deps import (
    BraveConfig,
    DeepSeekFetchConfig,
    DeepSeekSearchConfig,
    FetchDeps,
    FetchModel,
    JinaConfig,
    ReaderProvider,
    SearchDeps,
    SearchProvider,
)
from meiva.web.errors import FetchError

__all__ = ["fetch_deps", "fetch_deps_for", "search_deps", "search_deps_for"]


def search_deps() -> SearchDeps:
    return search_deps_for(sdk_settings.config_authority())


def search_deps_for(authority: ConfigAuthority) -> SearchDeps:
    timeout_s = authority.service_field_value("web_search_timeout_seconds")
    return SearchDeps(
        provider=cast(SearchProvider, authority.service_field_value("web_search_provider")),
        brave=BraveConfig(
            api_key=authority.service_field_value("brave_api_key"),
            endpoint=authority.service_field_value("web_brave_search_endpoint"),
            timeout_s=timeout_s,
            max_count=authority.service_field_value("web_max_search_results"),
        ),
        deepseek=DeepSeekSearchConfig(
            api_key=authority.service_field_value("deepseek_api_key"),
            endpoint=authority.service_field_value("web_deepseek_search_endpoint"),
            timeout_s=timeout_s,
            max_count=authority.service_field_value("web_deepseek_max_results"),
            pacer=RateLimiter(authority.service_field_value("web_deepseek_qps")),
        ),
        open_url=SYSTEM_URL_OPENER,
        scan=_scan,
    )


def fetch_deps() -> FetchDeps:
    authority = sdk_settings.config_authority()
    try:
        catalog = sdk_settings.model_catalog()
        overrides = sdk_settings.model_overrides()
    except RuntimeError as exc:
        raise FetchError(
            "web.fetch requires this SDK installation's model configuration (ModelCatalog), "
            "but it carries none; web.search needs no model"
        ) from exc
    return fetch_deps_for(authority, catalog=catalog, overrides=overrides)


def fetch_deps_for(
    authority: ConfigAuthority,
    *,
    catalog: ModelCatalog,
    overrides: ModelOverrides,
) -> FetchDeps:
    fetch_timeout_s = authority.service_field_value("web_fetch_timeout_seconds")
    return FetchDeps(
        reader=cast(ReaderProvider, authority.service_field_value("web_fetch_reader")),
        jina=JinaConfig(
            api_key=authority.service_field_value("jina_api_key"),
            reader_base=authority.service_field_value("web_jina_reader_base"),
            timeout_s=fetch_timeout_s,
        ),
        deepseek=DeepSeekFetchConfig(
            api_key=authority.service_field_value("deepseek_api_key"),
            endpoint=authority.service_field_value("web_deepseek_fetch_endpoint"),
            timeout_s=fetch_timeout_s,
            pacer=RateLimiter(authority.service_field_value("web_deepseek_qps")),
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

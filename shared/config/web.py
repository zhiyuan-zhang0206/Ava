"""Web config — WebSettings.

Split out of the former flat Settings god object; each field keeps its exact
env alias so the .env surface is unchanged. Aggregated by shared/config.
"""

from __future__ import annotations

from pydantic import Field, SecretStr

from shared.config._base import EnvSettings


class WebSettings(EnvSettings):
    web_search_timeout_seconds: float = Field(
        default=10.0,
        alias="AVA_WEB_SEARCH_TIMEOUT_SECONDS",
        description="Brave search HTTP timeout (seconds). Typical response < 1s; 10s covers occasional jitter.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    web_fetch_timeout_seconds: float = Field(
        default=30.0,
        alias="AVA_WEB_FETCH_TIMEOUT_SECONDS",
        description="Jina Reader fetch HTTP timeout (seconds). Headless browser + JS rendering is slow; 30s covers heavy SPA cold starts.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    web_max_search_results: int = Field(
        default=20,
        alias="AVA_WEB_MAX_RESULTS",
        description="Brave search max results per call (free tier caps at 20; higher values are rejected with 422).",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    web_brave_search_endpoint: str = Field(
        default="https://api.search.brave.com/res/v1/web/search",
        alias="AVA_WEB_BRAVE_SEARCH_ENDPOINT",
        description="Brave Search API endpoint used by `ava.web.search`. Override to point at a different search endpoint (e.g. a self-hosted relay or a provider mirror) without code changes. A custom endpoint receives the same auth as Brave: `X-Subscription-Token` with BRAVE_API_KEY.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    web_jina_reader_base: str = Field(
        default="https://r.jina.ai/",
        alias="AVA_WEB_JINA_BASE_URL",
        description="Jina Reader base URL used by `ava.web.fetch` (the target URL is appended after it). Must end with `/` — it is concatenated with the encoded target URL. Override to point at a different reader endpoint (e.g. a self-hosted mirror) without code changes. A custom endpoint receives the same auth as Jina: `Authorization: Bearer` with JINA_API_KEY.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    brave_api_key: SecretStr | None = Field(
        default=None,
        alias="BRAVE_API_KEY",
        description="Brave Search API key, used by `ava.web.search`.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": True,
            "scope": "cluster-pinned",
            "seed": True,
        },
    )

    web_fetch_model: str = Field(
        default="deepseek-v4-flash",
        alias="AVA_WEB_FETCH_MODEL",
        description="Model for `ava.web.fetch` page-content summarization. Separate from `understand_text_model` so the cheap fetch path does not use the agent's main model.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    web_fetch_reasoning: str = Field(
        default="none",
        alias="AVA_WEB_FETCH_REASONING",
        description="Reasoning effort for the `ava.web.fetch` summarization model. `none` skips reasoning entirely since the summarization prompt is simple.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    jina_api_key: SecretStr | None = Field(
        default=None,
        alias="JINA_API_KEY",
        description="Jina Reader API key (optional), used by `ava.web.fetch`. Empty = anonymous tier (20 RPM); with key = 500 RPM.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": True,
            "scope": "cluster-pinned",
            "seed": True,
        },
    )

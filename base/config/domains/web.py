from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr

from base.config.base import EnvSettings


class WebSettings(EnvSettings):
    web_search_timeout_seconds: float = Field(
        default=10.0,
        alias="MEIVA_WEB_SEARCH_TIMEOUT_SECONDS",
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
        alias="MEIVA_WEB_FETCH_TIMEOUT_SECONDS",
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
        alias="MEIVA_WEB_MAX_RESULTS",
        description="Brave search max results per call (free tier caps at 20; higher values are rejected with 422).",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    web_search_provider: Literal["brave", "deepseek"] = Field(
        default="brave",
        alias="MEIVA_WEB_SEARCH_PROVIDER",
        description=(
            "Search provider behind `meiva.web.search`. `brave` is the incumbent default; "
            "`deepseek` uses the DeepSeek Search tool, which needs DEEPSEEK_API_KEY (the same "
            "key the deepseek-* models use) and is bound by DeepSeek's account-level 5 QPS. "
            "The two differ in what they are good at rather than in a single score: DeepSeek "
            "favours primary and official sources and repeats one site's pages; Brave favours "
            "popular tutorials, keeps the requested language, and returns news/videos/"
            "discussions partitions that DeepSeek does not have. A provider switch needs a "
            "decision, not a config edit by habit."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    web_fetch_reader: Literal["jina", "deepseek"] = Field(
        default="jina",
        alias="MEIVA_WEB_FETCH_READER",
        description=(
            "Page reader behind `meiva.web.fetch`. `jina` runs Jina Reader (server-side headless "
            "browser, so it renders JavaScript and returns a bot-challenge or login-wall page as "
            "the body, which `meiva.web` detects and turns into a FetchError); `deepseek` runs "
            "the DeepSeek Fetch tool (Markdown body, needs DEEPSEEK_API_KEY, answers 403/404 "
            "when it cannot read the page, so there is no wall page to detect). The answer step "
            "and the injection check are unchanged by this switch."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    web_deepseek_search_endpoint: str = Field(
        default="https://api.deepseek.com/v1/tools/search",
        alias="MEIVA_WEB_DEEPSEEK_SEARCH_ENDPOINT",
        description=(
            "DeepSeek Search tool endpoint used when `web_search_provider` is `deepseek`. "
            "Override to point at a relay or mirror without code changes; it receives the same "
            "auth as DeepSeek (`Authorization: Bearer` with DEEPSEEK_API_KEY)."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    web_deepseek_fetch_endpoint: str = Field(
        default="https://api.deepseek.com/v1/tools/fetch",
        alias="MEIVA_WEB_DEEPSEEK_FETCH_ENDPOINT",
        description=(
            "DeepSeek Fetch tool endpoint used when `web_fetch_reader` is `deepseek`. Same "
            "override and auth rules as the search endpoint above."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    web_deepseek_max_results: int = Field(
        default=50,
        alias="MEIVA_WEB_DEEPSEEK_MAX_RESULTS",
        description=(
            "DeepSeek Search result cap per call. The tool rejects `count` above 50 and the "
            "price does not depend on it, so 50 is the ceiling the provider itself allows; the "
            "caller's `count` is clamped to it. Kept separate from `web_max_search_results` "
            "because Brave's ceiling (20) is lower and would silently cap DeepSeek results."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    web_deepseek_qps: float = Field(
        default=5.0,
        gt=0.0,
        alias="MEIVA_WEB_DEEPSEEK_QPS",
        description=(
            "Request rate `meiva.web` allows itself against the DeepSeek Search and Fetch "
            "tools, in requests per second. DeepSeek enforces 5 QPS per account for both tools "
            "and answers 429 above it; `meiva.web` spaces its request starts `1/web_deepseek_qps` "
            "apart so a 12-wide batch queues locally instead of retrying. Measuring a 10-query "
            "batch at concurrency 12: unthrottled 4 of 14 requests were 429 (2.1s), throttled to "
            "5 QPS none were (1.4s). Raise it only if DeepSeek raises the account limit."
        ),
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    web_brave_search_endpoint: str = Field(
        default="https://api.search.brave.com/res/v1/web/search",
        alias="MEIVA_WEB_BRAVE_SEARCH_ENDPOINT",
        description="Brave Search API endpoint used by `meiva.web.search`. Override to point at a different search endpoint (e.g. a self-hosted relay or a provider mirror) without code changes. A custom endpoint receives the same auth as Brave: `X-Subscription-Token` with BRAVE_API_KEY.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    web_jina_reader_base: str = Field(
        default="https://r.jina.ai/",
        alias="MEIVA_WEB_JINA_BASE_URL",
        description="Jina Reader base URL used by `meiva.web.fetch` (the target URL is appended after it). Must end with `/` — it is concatenated with the encoded target URL. Override to point at a different reader endpoint (e.g. a self-hosted mirror) without code changes. A custom endpoint receives the same auth as Jina: `Authorization: Bearer` with JINA_API_KEY.",
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
        description="Brave Search API key, used by `meiva.web.search`.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": True,
            "scope": "cluster-pinned",
        },
    )

    web_fetch_model: str = Field(
        default="deepseek-flash",
        alias="MEIVA_WEB_FETCH_MODEL",
        description="Model for `meiva.web.fetch` page-content summarization. Separate from `understand_text_model` so the cheap fetch path does not use the agent's main model.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    web_fetch_reasoning: str = Field(
        default="none",
        alias="MEIVA_WEB_FETCH_REASONING",
        description="Reasoning effort for the `meiva.web.fetch` summarization model. `none` skips reasoning entirely since the summarization prompt is simple.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    web_fetch_injection_check_enabled: bool = Field(
        default=True,
        alias="MEIVA_WEB_FETCH_INJECTION_CHECK_ENABLED",
        description="Check the page text `meiva.web.fetch` reads for instructions aimed at an AI reader, using a decision model, and attach a SECURITY note to the turn when the page looks like prompt injection. The page and its summary are never altered. The check only adds a warning: if the decision model is unavailable the fetch still succeeds. Needs `security_scan_enabled` and the decision model's API key.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    web_fetch_injection_model: str = Field(
        default="jev-1.13.0",
        alias="MEIVA_WEB_FETCH_INJECTION_MODEL",
        description="Decision model (an id from the decision-model catalog, not a chat model) that judges fetched page text for `web_fetch_injection_check_enabled`. An id the catalog does not list makes `meiva.web.fetch` fail instead of silently skipping the check.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    web_fetch_injection_threshold: float = Field(
        default=0.4,
        gt=0.0,
        lt=1.0,
        alias="MEIVA_WEB_FETCH_INJECTION_THRESHOLD",
        description="Probability (exclusive 0..1) at or above which a fetched page is reported as likely prompt injection. The page is judged in chunks; the highest chunk probability is compared. Lower values warn more often. The default 0.4 was calibrated together with the hijack-intent question wording of `meiva/sdk_surface/injection_check.py` (0 false positives on 209 benign pages, all 35 synthetic injections caught); recalibrate if either changes.",
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
        description="Jina Reader API key (optional), used by `meiva.web.fetch`. Empty = anonymous tier (20 RPM); with key = 500 RPM.",
        json_schema_extra={
            "restart_required": "agent",
            "writable": True,
            "sensitive": True,
            "scope": "cluster-pinned",
        },
    )

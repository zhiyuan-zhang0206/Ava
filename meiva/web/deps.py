# meiva:link meiva/web/docs/web.meiva.okf.md

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import SecretStr

from base.host.env.agent_slices import ModelOverrides
from base.host.net.ports import UrlOpener
from base.lm.catalog import ModelCatalog

__all__ = [
    "BraveConfig",
    "DeepSeekFetchConfig",
    "DeepSeekSearchConfig",
    "FetchDeps",
    "FetchModel",
    "JinaConfig",
    "ReaderProvider",
    "RequestPacer",
    "SearchDeps",
    "SearchProvider",
]


class RequestPacer(Protocol):
    def acquire(self) -> None: ...


SearchProvider = Literal["brave", "deepseek"]
ReaderProvider = Literal["jina", "deepseek"]


@dataclass(frozen=True)
class BraveConfig:
    """The Brave Search provider: key (None when unset), endpoint, per-request timeout, result cap."""

    api_key: SecretStr | None
    endpoint: str
    timeout_s: float
    max_count: int


@dataclass(frozen=True)
class DeepSeekSearchConfig:
    api_key: SecretStr | None
    endpoint: str
    timeout_s: float
    max_count: int
    pacer: RequestPacer


@dataclass(frozen=True)
class DeepSeekFetchConfig:
    api_key: SecretStr | None
    endpoint: str
    timeout_s: float
    pacer: RequestPacer


@dataclass(frozen=True)
class JinaConfig:
    """The Jina Reader provider: optional key, base URL (the page URL is appended), timeout."""

    api_key: SecretStr | None
    reader_base: str
    timeout_s: float


@dataclass(frozen=True)
class FetchModel:
    """The model that answers a prompt about a fetched page, and how its call is made."""

    model: str
    reasoning: str
    retry_attempts: int
    retry_delay_seconds: float
    retry_max_delay_seconds: float
    timeout_seconds: float
    llm_override: str
    catalog: ModelCatalog
    overrides: ModelOverrides


@dataclass(frozen=True)
class SearchDeps:
    provider: SearchProvider
    brave: BraveConfig
    deepseek: DeepSeekSearchConfig
    open_url: UrlOpener
    scan: Callable[[str, str], str]


@dataclass(frozen=True)
class FetchDeps:
    reader: ReaderProvider
    jina: JinaConfig
    deepseek: DeepSeekFetchConfig
    fetch_model: FetchModel
    open_url: UrlOpener
    build_chat_model: Callable[..., Any]
    scan: Callable[[str, str], str]
    begin_check: Callable[[str, str, str], Callable[[], None]]

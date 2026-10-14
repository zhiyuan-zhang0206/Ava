"""What the web operations need besides their arguments: configuration values and ports.

`ava/web/brave.py` and `ava/web/reader.py` read nothing ambient. The configured endpoints, keys
and timeouts arrive as small frozen config values, the network, the model factory and the
injection check as callables, so a test hands them a fake opener and the values it wants.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr

from base.host.env.agent_slices import ModelOverrides
from base.host.net.ports import UrlOpener
from base.lm.catalog import ModelCatalog

__all__ = ["BraveConfig", "FetchModel", "JinaConfig", "WebDeps"]


@dataclass(frozen=True)
class BraveConfig:
    """The Brave Search provider: key (None when unset), endpoint, per-request timeout, result cap."""

    api_key: SecretStr | None
    endpoint: str
    timeout_s: float
    max_count: int


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
class WebDeps:
    """The dependencies of `search_one` and `fetch_one`, nothing else."""

    brave: BraveConfig
    jina: JinaConfig
    fetch_model: FetchModel
    open_url: UrlOpener
    build_chat_model: Callable[..., Any]
    scan: Callable[[str, str], str]
    begin_check: Callable[[str, str, str], Callable[[], None]]

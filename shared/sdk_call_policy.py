"""Live SDK-event sampling, refreshed without network I/O on the SDK call path."""

from __future__ import annotations

import threading
import time

from pydantic import BaseModel, Field

from shared.log import logger

_REFRESH_SECONDS = 5.0


class SamplingPolicy(BaseModel):
    """Sampling is opt-in; the ratio is the inverse inclusion probability."""

    sampling_enabled: bool = False
    sample_every: int = Field(default=10, ge=1)


def _read_policy() -> SamplingPolicy:
    from shared.bootstrap import (
        config_source_is_local,
        fetch_bootstrap_config,
        should_fetch_from_gateway,
    )
    from shared.config import settings
    from shared.runtime_config import read_env_aliases

    if config_source_is_local() or not should_fetch_from_gateway():
        aliases = read_env_aliases()
    else:
        aliases = fetch_bootstrap_config(
            settings.gateway.gateway_url, timeout=2.0, attempts=1, role="runner"
        )
    defaults = SamplingPolicy()
    return SamplingPolicy.model_validate(
        {
            "sampling_enabled": aliases.get(
                "AVA_SDK_CALL_SAMPLING_ENABLED", defaults.sampling_enabled
            ),
            "sample_every": aliases.get("AVA_SDK_CALL_SAMPLE_EVERY", defaults.sample_every),
        }
    )


class _PolicyCache:
    def __init__(self) -> None:
        self.value: SamplingPolicy | None = None
        self.next_refresh = 0.0
        self.lock = threading.Lock()
        self.refreshing = False

    def read(self) -> SamplingPolicy:
        from shared.config import settings

        with self.lock:
            if self.value is None:
                self.value = SamplingPolicy(
                    sampling_enabled=settings.observability.sdk_call_sampling_enabled,
                    sample_every=settings.observability.sdk_call_sample_every,
                )
            if time.monotonic() >= self.next_refresh and not self.refreshing:
                self.refreshing = True
                threading.Thread(target=self.refresh, name="sdk-call-policy", daemon=True).start()
            return self.value

    def refresh(self) -> None:
        try:
            value = _read_policy()
            with self.lock:
                self.value = value
        except Exception:
            logger.bind(_no_emitter=True).warning(
                "SDK sampling config refresh failed; retaining the last valid policy",
                exc_info=True,
            )
        finally:
            with self.lock:
                self.next_refresh = time.monotonic() + _REFRESH_SECONDS
                self.refreshing = False


_cache = _PolicyCache()


def policy() -> SamplingPolicy:
    """Return the current snapshot; at most one background refresh per five seconds."""
    return _cache.read()

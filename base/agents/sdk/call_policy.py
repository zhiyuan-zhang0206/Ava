"""Live SDK-event sampling, refreshed without network I/O on the SDK call path."""

from __future__ import annotations

import contextlib
import math
import threading
import time
from collections.abc import Callable, Generator
from types import TracebackType

from pydantic import BaseModel, Field

from base.log import logger

_REFRESH_SECONDS = 5.0


class SamplingPolicy(BaseModel):
    """Sampling is opt-in; the ratio is the inverse inclusion probability."""

    sampling_enabled: bool = False
    sample_every: int = Field(default=10, ge=1)


def _read_policy() -> SamplingPolicy:
    from base.config import settings
    from base.host.env.bootstrap import (
        config_source_is_local,
        fetch_bootstrap_config,
        should_fetch_from_gateway,
    )
    from base.host.env.runtime_config import read_env_aliases

    if config_source_is_local() or not should_fetch_from_gateway():
        aliases = read_env_aliases()
    else:
        aliases = fetch_bootstrap_config(settings.gateway.gateway_url, timeout=2.0, attempts=1)
    defaults = SamplingPolicy()
    return SamplingPolicy.model_validate(
        {
            "sampling_enabled": aliases.get(
                "AVA_SDK_CALL_SAMPLING_ENABLED", defaults.sampling_enabled
            ),
            "sample_every": aliases.get("AVA_SDK_CALL_SAMPLE_EVERY", defaults.sample_every),
        }
    )


class _Refresh:
    """One retained refresh attempt; shutdown observes the actual worker."""

    def __init__(self, refresh: Callable[[], None]) -> None:
        self.refresh = refresh
        self.stopping = threading.Event()
        self.completed = threading.Event()
        self.error: BaseException | None = None
        self.traceback: TracebackType | None = None
        self.thread = threading.Thread(target=self.run, name="sdk-call-policy", daemon=True)
        self.thread.start()

    def run(self) -> None:
        try:
            if not self.stopping.is_set():
                self.refresh()
        except BaseException as exc:
            self.error = exc
            self.traceback = exc.__traceback__
            logger.bind(_no_emitter=True).opt(exception=exc).error(
                "SDK sampling refresh worker failed: {error}", error=exc
            )
        finally:
            self.completed.set()

    def stop(self, timeout: float) -> bool:
        self.stopping.set()
        self.thread.join(timeout=timeout)
        if self.thread.is_alive():
            return False
        if self.error is not None:
            self.error.with_traceback(self.traceback)
            raise self.error
        return True


class SamplingPolicyOwner:
    """The SDK installation's lazy policy and finite refresh lifetime."""

    def __init__(self, *, reader: Callable[[], SamplingPolicy] | None = None) -> None:
        self.reader = reader
        self.value: SamplingPolicy | None = None
        self.next_refresh = 0.0
        self.lock = threading.Lock()
        self.refreshing = False
        self.error: tuple[Exception, TracebackType | None] | None = None
        self.worker: _Refresh | None = None
        self.stopped = False

    def read(self) -> SamplingPolicy:
        from base.config import settings

        with self.lock:
            if self.stopped:
                raise RuntimeError("the SDK sampling owner has stopped")
            if self.value is None:
                self.value = SamplingPolicy(
                    sampling_enabled=settings.observability.sdk_call_sampling_enabled,
                    sample_every=settings.observability.sdk_call_sample_every,
                )
            if self.worker is not None and self.worker.error is not None:
                raise self.worker.error.with_traceback(self.worker.traceback)
            alive = self.worker is not None and self.worker.thread.is_alive()
            if time.monotonic() >= self.next_refresh and not self.refreshing and not alive:
                self.refreshing = True
                try:
                    self.worker = _Refresh(self.refresh)
                except BaseException:
                    self.refreshing = False
                    raise
            if self.worker is not None and self.worker.error is not None:
                raise self.worker.error.with_traceback(self.worker.traceback)
            if self.error is not None:
                error, traceback = self.error
                raise error.with_traceback(traceback)
            return self.value

    def stop(self, timeout: float = 2.0) -> bool:
        """Fence new reads; return False while the retained attempt remains alive.

        A later stop observes its original failure. A completed invalid refresh
        raises the same exception that subsequent SDK entries would receive.
        """
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("SDK sampling stop timeout must be finite and nonnegative")
        with self.lock:
            self.stopped = True
            worker = self.worker
        if worker is not None and not worker.stop(timeout):
            return False
        with self.lock:
            if self.error is not None:
                error, traceback = self.error
                raise error.with_traceback(traceback)
        return True

    def close(self) -> bool:
        """Report an unfinished finite stop; propagate completed original failures."""
        joined = self.stop()
        if not joined:
            logger.bind(_no_emitter=True).warning(
                "SDK sampling refresh unfinished at shutdown; worker retained",
                event="sdk_sampling_refresh_unfinished",
            )
        return joined

    @contextlib.contextmanager
    def execution(self) -> Generator[None, None, None]:
        """Collect refresh outcomes before the child forms its result envelope."""
        primary: BaseException | None = None
        primary_traceback: TracebackType | None = None
        try:
            yield
        except BaseException as exc:
            primary, primary_traceback = exc, exc.__traceback__
            raise
        finally:
            try:
                self.close()
            except BaseException as secondary:
                if primary is None:
                    raise
                if secondary is not primary:
                    primary.add_note(f"SDK sampling shutdown also failed: {secondary!r}")
                primary.with_traceback(primary_traceback)

    def refresh(self) -> None:
        transient_errors: tuple[type[Exception], ...] = ()
        status_error = None
        try:
            import httpx

            transient_errors = (
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
            )
            status_error = httpx.HTTPStatusError
            value = _read_policy() if self.reader is None else self.reader()
            with self.lock:
                self.value = value
                self.error = None
        except Exception as exc:
            expected = isinstance(exc, transient_errors) or (
                status_error is not None
                and isinstance(exc, status_error)
                and exc.response.status_code == 429
            )
            if expected:
                logger.bind(_no_emitter=True).opt(exception=True).warning(
                    "SDK sampling config fetch unavailable; retaining the last valid policy",
                )
            else:
                with self.lock:
                    self.error = (exc, exc.__traceback__)
                logger.bind(_no_emitter=True).opt(exception=True).error(
                    "SDK sampling config refresh failed; SDK calls will reject the invalid policy",
                )
        finally:
            with self.lock:
                self.next_refresh = time.monotonic() + _REFRESH_SECONDS
                self.refreshing = False


def policy(owner: SamplingPolicyOwner) -> SamplingPolicy:
    """Read the supplied installation's snapshot, with one attempt per five seconds."""
    return owner.read()

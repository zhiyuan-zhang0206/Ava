"""Live sampling uses validated snapshots and never blocks SDK calls on a fetch."""

import builtins
import os
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from base.agents.sdk import call_policy
from base.agents.sdk.call_policy import SamplingPolicy


@pytest.mark.parametrize("gateway", [True, False])
def test_local_policy_reads_live_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gateway: bool
) -> None:
    from base.host.env import bootstrap, runtime_config

    env_file = tmp_path / ".env"
    monkeypatch.setattr(bootstrap, "config_source_is_local", lambda: gateway)
    monkeypatch.setattr(bootstrap, "should_fetch_from_gateway", lambda: False)
    monkeypatch.setattr(runtime_config, "env_file_path", lambda: env_file)
    env_file.write_text("AVA_SDK_CALL_SAMPLING_ENABLED=true\nAVA_SDK_CALL_SAMPLE_EVERY=4\n")
    assert call_policy._read_policy() == SamplingPolicy(sampling_enabled=True, sample_every=4)
    env_file.write_text("AVA_SDK_CALL_SAMPLING_ENABLED=false\nAVA_SDK_CALL_SAMPLE_EVERY=7\n")
    assert call_policy._read_policy() == SamplingPolicy(sampling_enabled=False, sample_every=7)
    env_file.write_text("")
    assert call_policy._read_policy() == SamplingPolicy()


def test_remote_policy_refreshes_without_waiting_for_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = call_policy.SamplingPolicyOwner()
    cache.value = SamplingPolicy()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def read() -> SamplingPolicy:
        started.set()
        assert release.wait(5)
        return SamplingPolicy(sampling_enabled=True, sample_every=3)

    monkeypatch.setattr(call_policy, "_read_policy", read)
    original = cache.refresh

    def refresh() -> None:
        try:
            original()
        finally:
            finished.set()

    monkeypatch.setattr(cache, "refresh", refresh)
    try:
        assert cache.read() == SamplingPolicy()
        assert started.wait(2)
        assert cache.read() == SamplingPolicy()
    finally:
        release.set()
        assert finished.wait(2)
    assert cache.read() == SamplingPolicy(sampling_enabled=True, sample_every=3)


def test_failed_refresh_preserves_policy_and_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = call_policy.SamplingPolicyOwner()
    cache.value = SamplingPolicy(sampling_enabled=True, sample_every=5)

    def read() -> SamplingPolicy:
        raise httpx.ConnectError("gateway unavailable")

    reports: list[Any] = []
    monkeypatch.setattr(call_policy, "_read_policy", read)
    from loguru import logger

    sink = logger.add(lambda message: reports.append(message.record))
    try:
        cache.refresh()
    finally:
        logger.remove(sink)
    assert cache.value.sample_every == 5
    warned = [row for row in reports if row["level"].name == "WARNING"]
    assert warned and warned[0]["exception"] is not None  # the traceback rides it (#4979)
    assert warned[0]["exception"].type is httpx.ConnectError
    assert warned[0]["extra"]["_no_emitter"] is True
    assert not cache.refreshing


@pytest.mark.parametrize("value", [0, -1, 1.5])
def test_ratio_rejects_invalid_values(value: Any) -> None:
    with pytest.raises(ValidationError):
        SamplingPolicy(sample_every=value)


def test_enrolled_process_reads_gateway_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    from base.host.env import bootstrap

    monkeypatch.setattr(bootstrap, "config_source_is_local", lambda: False)
    monkeypatch.setattr(bootstrap, "should_fetch_from_gateway", lambda: True)
    requests: list[dict[str, Any]] = []

    def fetch(_url: str, **kwargs: Any) -> dict[str, str]:
        requests.append(kwargs)
        return {"AVA_SDK_CALL_SAMPLING_ENABLED": "true", "AVA_SDK_CALL_SAMPLE_EVERY": "2"}

    monkeypatch.setattr(bootstrap, "fetch_bootstrap_config", fetch)
    assert call_policy._read_policy() == SamplingPolicy(sampling_enabled=True, sample_every=2)
    assert requests == [{"timeout": 2.0, "attempts": 1}]


def _http_failure(status: int) -> httpx.HTTPStatusError:
    response = httpx.Response(status, request=httpx.Request("GET", "https://gateway/api/bootstrap"))
    return httpx.HTTPStatusError("bootstrap rejected", request=response.request, response=response)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("offline"),
        httpx.ReadError("connection lost"),
        httpx.ReadTimeout("timed out"),
        httpx.PoolTimeout("busy"),
        httpx.RemoteProtocolError("connection closed"),
        _http_failure(429),
    ],
)
def test_expected_fetch_failure_retains_the_valid_snapshot(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    from base.telemetry import emitter

    def initialize(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("a sampling fetch failure must not initialize an event pipeline")

    monkeypatch.setattr(emitter, "init_telemetry", initialize)
    cache = call_policy.SamplingPolicyOwner()
    snapshot = SamplingPolicy(sampling_enabled=True, sample_every=5)
    cache.value = snapshot

    def read() -> SamplingPolicy:
        raise error

    monkeypatch.setattr(call_policy, "_read_policy", read)
    cache.refresh()
    assert cache.read() is snapshot


@pytest.mark.parametrize(
    "error",
    [
        _http_failure(401),
        _http_failure(500),
        httpx.LocalProtocolError("invalid request"),
        httpx.UnsupportedProtocol("bad URL"),
        PermissionError("config unreadable"),
        TypeError("invalid config shape"),
        RuntimeError("reader bug"),
    ],
)
def test_unexpected_refresh_error_persists_until_a_valid_snapshot(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    cache = call_policy.SamplingPolicyOwner()
    cache.value = SamplingPolicy(sample_every=5)

    def read() -> SamplingPolicy:
        raise error

    monkeypatch.setattr(call_policy, "_read_policy", read)
    cache.refresh()
    depths: list[int] = []
    for _ in range(2):
        with pytest.raises(type(error)) as caught:
            cache.read()
        assert caught.value is error
        depths.append(sum(1 for _ in traceback.walk_tb(caught.value.__traceback__)))
    assert depths[0] == depths[1]  # Repeated callers do not accumulate each other's stacks.

    def unavailable() -> SamplingPolicy:
        raise httpx.ConnectError("still offline")

    monkeypatch.setattr(call_policy, "_read_policy", unavailable)
    cache.refresh()
    with pytest.raises(type(error)):
        cache.read()  # An outage must not clear an already-invalid configuration.
    recovered = SamplingPolicy(sampling_enabled=True, sample_every=7)
    monkeypatch.setattr(call_policy, "_read_policy", lambda: recovered)
    cache.refresh()
    assert cache.read() is recovered


def test_invalid_schema_is_not_reported_as_a_retained_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = call_policy.SamplingPolicyOwner()
    cache.value = SamplingPolicy()
    monkeypatch.setattr(call_policy, "_read_policy", lambda: SamplingPolicy(sample_every=0))
    cache.refresh()
    with pytest.raises(ValidationError):
        cache.read()


def test_failed_policy_read_still_starts_one_refresh_when_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = call_policy.SamplingPolicyOwner()
    cache.value = SamplingPolicy()
    error = TypeError("invalid policy")

    def fail() -> SamplingPolicy:
        raise error

    clock = [10.0]
    monkeypatch.setattr(call_policy.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(call_policy, "_read_policy", fail)
    cache.refresh()
    assert cache.next_refresh == 15.0
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    snapshot = SamplingPolicy(sample_every=9)

    fetches: list[str] = []

    def recover() -> SamplingPolicy:
        fetches.append("fetch")
        started.set()
        assert release.wait(5)
        return snapshot

    monkeypatch.setattr(call_policy, "_read_policy", recover)
    original = cache.refresh

    def refresh() -> None:
        try:
            original()
        finally:
            finished.set()

    monkeypatch.setattr(cache, "refresh", refresh)
    clock[0] = 15.0
    try:
        for _ in range(2):
            with pytest.raises(TypeError, match="invalid policy"):
                cache.read()
        assert started.wait(2)
        assert cache.refreshing
        assert fetches == ["fetch"]
    finally:
        release.set()
        assert finished.wait(2)
    assert cache.read() is snapshot
    assert cache.next_refresh == 20.0


def test_sdk_telemetry_cold_import_does_not_load_httpx(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; import base.agents.sdk.telemetry; "
            "assert 'httpx' not in sys.modules; assert 'httpcore' not in sys.modules",
        ],
        cwd=tmp_path,
        env={**os.environ, "AVA_HOME": str(tmp_path / "home"), "AVA_CONFIG_FETCH": "skip"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_refresh_dependency_import_failure_reaches_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = call_policy.SamplingPolicyOwner()
    cache.value = SamplingPolicy()
    original_import = builtins.__import__

    def importing(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "httpx":
            raise ImportError("HTTPX unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    cache.refresh()
    with pytest.raises(ImportError, match="HTTPX unavailable"):
        cache.read()


def test_invalid_refresh_dependency_classification_reaches_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = call_policy.SamplingPolicyOwner()
    cache.value = SamplingPolicy()
    monkeypatch.delattr(httpx, "HTTPStatusError")
    cache.refresh()
    with pytest.raises(AttributeError, match="HTTPStatusError"):
        cache.read()

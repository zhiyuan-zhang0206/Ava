"""Live sampling uses validated snapshots and never blocks SDK calls on a fetch."""

import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from shared import sdk_call_policy
from shared.sdk_call_policy import SamplingPolicy


@pytest.mark.parametrize("gateway", [True, False])
def test_local_policy_reads_live_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gateway: bool
) -> None:
    from shared import bootstrap, runtime_config

    env_file = tmp_path / ".env"
    monkeypatch.setattr(bootstrap, "config_source_is_local", lambda: gateway)
    monkeypatch.setattr(bootstrap, "should_fetch_from_gateway", lambda: False)
    monkeypatch.setattr(runtime_config, "env_file_path", lambda: env_file)
    env_file.write_text("AVA_SDK_CALL_SAMPLING_ENABLED=true\nAVA_SDK_CALL_SAMPLE_EVERY=4\n")
    assert sdk_call_policy._read_policy() == SamplingPolicy(sampling_enabled=True, sample_every=4)
    env_file.write_text("AVA_SDK_CALL_SAMPLING_ENABLED=false\nAVA_SDK_CALL_SAMPLE_EVERY=7\n")
    assert sdk_call_policy._read_policy() == SamplingPolicy(sampling_enabled=False, sample_every=7)
    env_file.write_text("")
    assert sdk_call_policy._read_policy() == SamplingPolicy()


def test_remote_policy_refreshes_without_waiting_for_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = sdk_call_policy._PolicyCache()
    cache.value = SamplingPolicy()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def read() -> SamplingPolicy:
        started.set()
        assert release.wait(5)
        return SamplingPolicy(sampling_enabled=True, sample_every=3)

    monkeypatch.setattr(sdk_call_policy, "_read_policy", read)
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
    cache = sdk_call_policy._PolicyCache()
    cache.value = SamplingPolicy(sampling_enabled=True, sample_every=5)

    def read() -> SamplingPolicy:
        raise OSError("gateway unavailable")

    reports: list[Any] = []
    monkeypatch.setattr(sdk_call_policy, "_read_policy", read)
    from loguru import logger

    sink = logger.add(lambda message: reports.append(message.record))
    try:
        cache.refresh()
    finally:
        logger.remove(sink)
    assert cache.value.sample_every == 5
    assert any(row["level"].name == "WARNING" for row in reports)
    assert not cache.refreshing


@pytest.mark.parametrize("value", [0, -1, 1.5])
def test_ratio_rejects_invalid_values(value: Any) -> None:
    with pytest.raises(ValidationError):
        SamplingPolicy(sample_every=value)


def test_enrolled_process_reads_gateway_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared import bootstrap

    monkeypatch.setattr(bootstrap, "config_source_is_local", lambda: False)
    monkeypatch.setattr(bootstrap, "should_fetch_from_gateway", lambda: True)
    requests: list[dict[str, Any]] = []

    def fetch(_url: str, **kwargs: Any) -> dict[str, str]:
        requests.append(kwargs)
        return {"AVA_SDK_CALL_SAMPLING_ENABLED": "true", "AVA_SDK_CALL_SAMPLE_EVERY": "2"}

    monkeypatch.setattr(bootstrap, "fetch_bootstrap_config", fetch)
    assert sdk_call_policy._read_policy() == SamplingPolicy(sampling_enabled=True, sample_every=2)
    assert requests == [{"timeout": 2.0, "attempts": 1, "role": "runner"}]

"""Explicit configuration owners keep overlays, views and build gates separate."""

from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, get_ident
from unittest.mock import patch

import pytest

import base.config as facade
from base.config import ConfigBoot, ConfigBuildWaitTimeoutError, _full, _lite
from base.host.env.dotenv_boot import EnvBootResult


@pytest.fixture(autouse=True)
def _isolated_owner_environment(tmp_path: Path) -> Iterator[None]:
    # The real delivery pass updates/drops env aliases; restore its whole input.
    with patch.dict(os.environ):
        os.environ["AVA_HOME"] = str(tmp_path)
        (tmp_path / ".env").write_text(
            "AVA_MACHINE_SERVE_GATEWAY=true\n"
            "AVA_DB_URL=postgresql://test:test@127.0.0.1:1/initial\n"
            "AVA_REDIS_URL=redis://127.0.0.1:1/0\n",
            encoding="utf-8",
        )
        yield


def test_constructor_and_skip_boot_do_not_deliver_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_load() -> None:
        raise AssertionError("construction/skip boot must not load environment")

    monkeypatch.setattr(_lite, "load_ava_env", unexpected_load)
    monkeypatch.setenv("AVA_CONFIG_FETCH", "skip")
    monkeypatch.setenv("AVA_CONFIG_BOOT", "eager")
    owner = ConfigBoot()
    owner.boot()
    assert not owner.boot_state()["prepared"]
    assert not owner.is_full()


def test_two_real_models_keep_stable_views_overlays_and_live_readers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = ConfigBoot(), ConfigBoot()
    first_view, second_view = first.view, second.view
    first.set_field("llm_model", "first-model")
    second.set_field("llm_model", "second-model")
    first.set_field("skills_to_expand_at_start", ["first-skill"])
    second.set_field("skills_to_expand_at_start", ["second-skill"])

    def first_reader():
        return first.view.lm.llm_model

    def second_reader():
        return second.view.lm.llm_model

    assert (first_reader(), second_reader()) == ("first-model", "second-model")
    assert (first.is_full(), second.is_full()) == (False, False)
    legacy_settings = facade.settings
    first_model = first.ensure_eager()
    second_model = second.ensure_eager()
    assert isinstance(first_model, _full.Settings)
    assert isinstance(second_model, _full.Settings)
    assert first_model is not second_model
    assert facade.settings is legacy_settings
    assert (first.view, second.view) == (first_view, second_view)
    assert first_view.agent.skills_to_expand_at_start == ["first-skill"]
    assert second_view.agent.skills_to_expand_at_start == ["second-skill"]
    first.set_field("llm_model", "first-update")
    second_view.lm.llm_model = "second-update"
    monkeypatch.setattr(facade, "settings", object())
    assert (first_reader(), second_reader()) == ("first-update", "second-update")
    assert first.get_field("llm_model") == "first-update"
    assert first.ensure_eager() is first_model
    assert first.boot_state()["upgrades"] == second.boot_state()["upgrades"] == 1


def test_lite_derived_default_uses_its_owner() -> None:
    first, second = ConfigBoot(), ConfigBoot()
    first.set_field("telemetry_otlp_port", 3101)
    second.set_field("telemetry_otlp_port", 3102)
    assert first.view.observability.telemetry_otlp_endpoint == "http://127.0.0.1:3101"
    assert second.view.observability.telemetry_otlp_endpoint == "http://127.0.0.1:3102"
    assert (first.is_full(), second.is_full()) == (False, False)


def test_full_view_assignment_and_data_plane_refresh_affect_only_its_owner(
    tmp_path: Path,
) -> None:
    first, second = ConfigBoot(), ConfigBoot()
    first_view = first.view
    first_view.lm.llm_model = "assigned-model"
    assert first.is_full() and not second.is_full()
    second.ensure_eager()
    first_general = first_view.general
    second_data_plane = second.view.data_plane
    old_url = second_data_plane.db_url
    (tmp_path / ".env").write_text(
        "AVA_MACHINE_SERVE_GATEWAY=true\n"
        "AVA_DB_URL=postgresql://new:new@127.0.0.1:1/refreshed\n"
        "AVA_REDIS_URL=redis://127.0.0.1:1/0\n",
        encoding="utf-8",
    )
    first.refresh_data_plane_settings()
    assert (
        str(first_view.data_plane.db_url)
        == "postgresql://new:new@127.0.0.1:1/refreshed?hostaddr=127.0.0.1"
    )
    assert first_view.general is first_general
    assert second.view.data_plane is second_data_plane
    assert second.view.data_plane.db_url == old_url


def test_profile_rejection_remains_lazy_and_instance_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AVA_CONFIG_BOOT", raising=False)
    monkeypatch.delenv("AVA_CONFIG_FETCH", raising=False)
    monkeypatch.setenv("AVA_PROCESS_PROFILE", "runner")
    limited = ConfigBoot()
    limited.boot()
    monkeypatch.delenv("AVA_PROCESS_PROFILE")
    complete = ConfigBoot()
    complete.boot()
    assert not limited.view.has_domain("alerts")
    assert complete.view.has_domain("alerts")
    with pytest.raises(AttributeError, match="profile"):
        _ = limited.view.alerts
    assert not limited.is_full()


def test_owner_build_gate_is_bounded_and_does_not_block_another_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked, independent = ConfigBoot(), ConfigBoot()
    entered, release = Event(), Event()
    builder_id: list[int] = []
    build = _full.build

    def controlled_build(*, env_boot: EnvBootResult) -> _full.FullBundle:
        if get_ident() in builder_id:
            entered.set()
            assert release.wait(5)
        return build(env_boot=env_boot)

    def blocked_build():
        builder_id.append(get_ident())
        return blocked.ensure_eager()

    monkeypatch.setattr(_full, "build", controlled_build)
    monkeypatch.setattr(_lite, "_BUILD_WAIT_TIMEOUT_SECONDS", 0.05)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(blocked_build)
        try:
            assert entered.wait(5)
            independent.ensure_eager()
            assert independent.is_full()
            with pytest.raises(ConfigBuildWaitTimeoutError, match="wait bound"):
                _ = blocked.view.model_dump
            assert not blocked.is_full()
        finally:
            release.set()
        assert task.result(timeout=5) is blocked.ensure_eager()
    assert callable(blocked.view.model_dump)


def test_same_thread_build_reads_lite_without_recursive_build_and_retries_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = ConfigBoot()
    owner.set_field("llm_model", "pending-model")
    build = _full.build
    calls = 0

    def failing_then_successful_build(*, env_boot: EnvBootResult) -> _full.FullBundle:
        nonlocal calls
        calls += 1
        assert owner.view.lm.llm_model == "pending-model"
        with pytest.raises(AttributeError, match="in flight"):
            _ = owner.view.model_dump
        if calls == 1:
            raise ValueError("injected build failure")
        return build(env_boot=env_boot)

    monkeypatch.setattr(_full, "build", failing_then_successful_build)
    with pytest.raises(ValueError, match="injected build failure"):
        owner.ensure_eager()
    assert not owner.is_full()
    assert owner.boot_state()["pending_count"] == 1
    owner.ensure_eager()
    assert owner.view.lm.llm_model == "pending-model"
    assert calls == 2
    assert owner.boot_state()["pending_count"] == 0


@pytest.mark.parametrize("probe", ["profile", "has_domain"])
def test_skip_boot_profile_probes_prepare_without_full_build(
    monkeypatch: pytest.MonkeyPatch, probe: str
) -> None:
    monkeypatch.setenv("AVA_CONFIG_FETCH", "skip")
    monkeypatch.setenv("AVA_CONFIG_BOOT", "eager")
    monkeypatch.setenv("AVA_PROCESS_PROFILE", "agent")
    owner = ConfigBoot()
    owner.boot()
    assert not owner.boot_state()["prepared"]
    if probe == "profile":
        assert owner.view.profile == "agent"
    else:
        assert not owner.view.has_domain("alerts")
    assert owner.boot_state()["prepared"]
    assert owner.view.profile == "agent"
    assert owner.view.has_domain("lm")
    assert not owner.view.has_domain("alerts")
    assert not owner.is_full()


def test_skip_agent_profile_selects_deferred_authority_for_other_domains(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from base.config.service_read import ConfigAuthority

    monkeypatch.setenv("AVA_CONFIG_FETCH", "skip")
    monkeypatch.setenv("AVA_PROCESS_PROFILE", "agent")
    owner = ConfigBoot()
    owner.boot()
    builds = 0

    def build_complete() -> _full.Settings:
        nonlocal builds
        builds += 1
        return _full.Settings(profile=None)

    # The real root selects this branch before any field has prepared the owner.
    if owner.view.profile is None:
        authority = ConfigAuthority(owner.view, owner.view, tmp_path / ".env")
    else:
        authority = ConfigAuthority.deferred(
            runtime=owner.view, build_all_domains=build_complete, env_path=tmp_path / ".env"
        )
    assert builds == 0
    assert not owner.is_full()
    value = authority.service_field_value("provider_guard_balance_enabled")
    assert isinstance(value, bool)
    assert builds == 1
    assert authority.service_field_value("provider_guard_balance_enabled") == value
    assert builds == 1
    assert not owner.is_full()

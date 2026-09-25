"""Preparation follows the desired roster without downloading unrelated services."""

from pathlib import Path

import pytest

from cli.commands import _converge, _lgtm_native
from cli.commands._converge_spec import ConvergeCtx, ConvergeStep


def test_preparation_filters_service_consumers_but_keeps_shared_host_steps(tmp_path: Path) -> None:
    calls: list[str] = []

    def shared(ctx: ConvergeCtx) -> None:
        assert ctx.services == frozenset({"loki"})
        calls.append("shared")

    def backend(_ctx: ConvergeCtx) -> None:
        calls.append("backend")

    def unrelated(_ctx: ConvergeCtx) -> None:
        pytest.fail("Unselected collector must not prepare assets")

    steps = (
        ConvergeStep("shared", shared),
        ConvergeStep("backend", backend, services=frozenset({"loki", "grafana"})),
        ConvergeStep("collector", unrelated, services=frozenset({"otel-collector"})),
    )
    _converge.converge_host(
        tmp_path,
        frozenset({"gateway"}),
        ava_home=tmp_path / "home",
        steps=steps,
        services=frozenset({"loki"}),
    )
    assert calls == ["shared", "backend"]


def test_native_preparation_downloads_only_selected_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloads: list[str] = []

    def assets(_repo: Path) -> dict[str, dict[str, str]]:
        return {name: {"version": "test"} for name in _lgtm_native.BACKENDS}

    def download(name: str, _version: str, _asset: dict[str, str], _native: Path) -> None:
        downloads.append(name)

    def render(_repo: Path, _native: Path, _home: Path) -> None:
        pass

    def no_loki(_home: Path) -> None:
        pytest.fail("Unselected Loki must not require its executable or validator")

    monkeypatch.setattr(_lgtm_native, "platform_tag", lambda: "darwin_arm64")
    monkeypatch.setattr(_lgtm_native, "_load_versions", assets)
    monkeypatch.setattr(_lgtm_native, "_download_and_verify", download)
    monkeypatch.setattr(_lgtm_native, "_render_configs", render)
    monkeypatch.setattr(_lgtm_native, "_verify_loki", no_loki)
    monkeypatch.setattr(_lgtm_native, "_render_grafana_admin_password", no_loki)
    _lgtm_native.ensure_lgtm_native(tmp_path, tmp_path / "home", services=frozenset({"prometheus"}))
    assert downloads == ["prometheus"]

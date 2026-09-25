"""Observability toggles change intent through the sole start lifecycle."""

from pathlib import Path

import pytest

from cli.commands import _lgtm
from shared.service_selection import ServiceSelection


def _wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, selection: ServiceSelection
) -> tuple[Path, list[dict[str, object]]]:
    marker = tmp_path / "lgtm-host"
    monkeypatch.setattr(_lgtm, "lgtm_host_marker", lambda: marker)
    monkeypatch.setattr("shared.service_selection.read_selection", lambda: selection)
    calls: list[dict[str, object]] = []

    def start(**kwargs: object) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr("cli.commands.start.cmd_start", start)
    return marker, calls


def test_on_preserves_explicit_other_service_choices(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker, calls = _wire(monkeypatch, tmp_path, ServiceSelection("only", frozenset({"ops"})))
    assert _lgtm.cmd_lgtm_on() == 0
    assert marker.exists()
    assert calls == [{"only_services": ("grafana", "loki", "ops", "prometheus")}]


def test_off_disables_backends_even_on_role_declared_station(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker, calls = _wire(monkeypatch, tmp_path, ServiceSelection("except", frozenset({"browser"})))
    marker.touch()
    data = tmp_path / "loki-data"
    data.write_bytes(b"durable history")
    assert _lgtm.cmd_lgtm_off() == 0
    assert not marker.exists()
    assert calls == [
        {"disabled_services": ("browser", "grafana", "loki", "prometheus"), "all_services": False}
    ]
    assert data.read_bytes() == b"durable history"


def test_off_only_backend_allowlist_does_not_enable_all_services(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    _marker, calls = _wire(
        monkeypatch, tmp_path, ServiceSelection("only", frozenset(_lgtm.BACKENDS))
    )
    monkeypatch.setattr(
        "ops.roster.build_services",
        lambda: tuple(
            SimpleNamespace(session=n) for n in ("gateway", "loki", "prometheus", "grafana")
        ),
    )
    assert _lgtm.cmd_lgtm_off() == 0
    assert calls == [{"disabled_services": ("gateway", "loki", "prometheus", "grafana")}]


def test_normal_start_refusal_is_not_reported_as_toggle_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _wire(monkeypatch, tmp_path, ServiceSelection("except", frozenset()))

    def refuse(**_kwargs: object) -> int:
        return 1

    monkeypatch.setattr("cli.commands.start.cmd_start", refuse)
    assert _lgtm.cmd_lgtm_on() == 1

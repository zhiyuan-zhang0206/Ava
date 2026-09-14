"""services.ava_root.probes: the code-side probe registry.

Two registration paths: specs that declare a `healthcheck_module` (the same
membership rule the watchdog applies), and static `register` / `register_ref`
entries for units whose probe never derives from a spec.
"""

from __future__ import annotations

import importlib
import logging
import sys
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from services.ava_root import probes
from services.ava_root.probes import ProbeError, ProbeRegistry
from shared.daemon_health import DaemonProbe

_PROBE_MODULE = """
    from shared.daemon_health import DaemonProbe

    def probe() -> DaemonProbe:
        return DaemonProbe.up("wired")
"""


def _alive() -> DaemonProbe:
    return DaemonProbe.up("ok")


def _down() -> DaemonProbe:
    return DaemonProbe.down("no")


@dataclass
class _Spec:
    """A ProbeSource-shaped stand-in (the real spec type lives in ops)."""

    session: str
    healthcheck_module: str | None
    identity_probe: Callable[[], DaemonProbe] | None


def _write_probe_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    (tmp_path / f"{name}.py").write_text(textwrap.dedent(_PROBE_MODULE))
    monkeypatch.syspath_prepend(str(tmp_path))  # pyright: ignore[reportUnknownMemberType]
    importlib.invalidate_caches()


# -- direct registration -------------------------------------------------------


def test_register_and_resolve_direct_probes() -> None:
    registry = ProbeRegistry()
    registry.register("alpha", _alive)
    registry.register("beta", _down)
    assert registry.unit_ids() == ("alpha", "beta")
    assert registry.resolve("alpha")().alive is True
    assert registry.resolve("beta")().alive is False


def test_duplicate_registration_rejected() -> None:
    registry = ProbeRegistry()
    registry.register("alpha", _alive)
    with pytest.raises(ProbeError, match="already registered"):
        registry.register("alpha", _down)


def test_empty_id_rejected() -> None:
    registry = ProbeRegistry()
    with pytest.raises(ProbeError, match="non-empty"):
        registry.register("", _alive)
    assert registry.unit_ids() == ()


def test_resolve_unknown_unit_rejected() -> None:
    with pytest.raises(ProbeError, match="no probe registered"):
        ProbeRegistry().resolve("ghost")


# -- lazy references -----------------------------------------------------------


def test_register_ref_resolves_lazily(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_probe_module(tmp_path, monkeypatch, "tmp_probe_alpha")
    registry = ProbeRegistry()
    registry.register_ref("alpha", "tmp_probe_alpha:probe")
    assert "tmp_probe_alpha" not in sys.modules
    probe = registry.resolve("alpha")
    assert probe().alive is True
    assert "tmp_probe_alpha" in sys.modules


def test_register_ref_validates_format_eagerly() -> None:
    registry = ProbeRegistry()
    for bad in ("nocolon", "mod:", ":attr", "a:b:c"):
        with pytest.raises(ProbeError, match="module:attribute"):
            registry.register_ref("alpha", bad)
    assert registry.unit_ids() == ()


def test_register_ref_unresolvable_module() -> None:
    registry = ProbeRegistry()
    registry.register_ref("alpha", "tmp_probe_never_created:probe")
    with pytest.raises(ProbeError, match="cannot import"):
        registry.resolve("alpha")


def test_register_ref_missing_attribute_and_non_callable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tmp_probe_attrs.py").write_text("value = 3\n")
    monkeypatch.syspath_prepend(str(tmp_path))  # pyright: ignore[reportUnknownMemberType]
    importlib.invalidate_caches()
    registry = ProbeRegistry()
    registry.register_ref("alpha", "tmp_probe_attrs:ghost")
    with pytest.raises(ProbeError, match="no attribute"):
        registry.resolve("alpha")
    registry.register_ref("beta", "tmp_probe_attrs:value")
    with pytest.raises(ProbeError, match="non-callable"):
        registry.resolve("beta")


def test_ref_resolution_is_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_probe_module(tmp_path, monkeypatch, "tmp_probe_cached")
    calls: list[str] = []
    real_resolve = probes._resolve_ref

    def counting(ref: str) -> Callable[[], DaemonProbe]:
        calls.append(ref)
        return real_resolve(ref)

    monkeypatch.setattr(probes, "_resolve_ref", counting)
    registry = ProbeRegistry()
    registry.register_ref("alpha", "tmp_probe_cached:probe")
    assert registry.resolve("alpha") is registry.resolve("alpha")
    assert calls == ["tmp_probe_cached:probe"]


def test_failed_ref_resolution_retries_on_next_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ProbeRegistry()
    registry.register_ref("alpha", "tmp_probe_late:probe")
    with pytest.raises(ProbeError):
        registry.resolve("alpha")
    _write_probe_module(tmp_path, monkeypatch, "tmp_probe_late")
    assert registry.resolve("alpha")().alive is True


# -- spec-derived registration --------------------------------------------------


def test_register_specs_membership_gate(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="services.ava_root.probes")
    registry = ProbeRegistry()
    registry.register_specs(
        [
            _Spec("kept", "svc.healthcheck", _alive),
            _Spec("no-module", None, _alive),
            _Spec("no-probe", "svc.healthcheck2", None),
        ]
    )
    assert registry.unit_ids() == ("kept",)
    assert registry.resolve("kept") is _alive
    assert "no-probe" in caplog.text


def test_register_specs_duplicate_unit_rejected() -> None:
    registry = ProbeRegistry()
    with pytest.raises(ProbeError, match="already registered"):
        registry.register_specs(
            [
                _Spec("dup", "svc.healthcheck", _alive),
                _Spec("dup", "svc.healthcheck", _down),
            ]
        )

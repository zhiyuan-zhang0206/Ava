"""services.ava_root.wiring: the daemon's optional deployment-hook contract."""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest

from services.ava_root.manifest import UnitRegistry
from services.ava_root.supervisor import Supervisor
from services.ava_root.wiring import (
    WiringContext,
    WiringError,
    load_wiring,
    start_participants,
    stop_participants,
)


def _context(tmp_path: Path) -> WiringContext:
    registry = UnitRegistry([])
    return WiringContext(
        supervisor=Supervisor(registry, log_dir=tmp_path / "logs"),
        registry=registry,
        run_dir=tmp_path,
        log_dir=tmp_path / "logs",
    )


class _Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def start(self) -> None:
        self.events.append("start")

    async def stop(self) -> None:
        self.events.append("stop")


def _install(monkeypatch: pytest.MonkeyPatch, name: str, attr: str, value: object) -> None:
    module = types.ModuleType(name)
    setattr(module, attr, value)
    monkeypatch.setitem(sys.modules, name, module)


def _returns(value: object) -> Callable[[WiringContext], object]:
    """A typed wiring factory returning `value` regardless of the context."""

    def factory(_context: WiringContext) -> object:
        return value

    return factory


def test_none_spec_yields_no_participants(tmp_path: Path) -> None:
    assert load_wiring(None, _context(tmp_path)) == ()


@pytest.mark.parametrize("spec", ["no_colon", ":attr", "mod:", "mod:attr:extra"])
def test_malformed_reference_rejected(tmp_path: Path, spec: str) -> None:
    with pytest.raises(WiringError, match="module:attribute"):
        load_wiring(spec, _context(tmp_path))


def test_import_failure_and_missing_attribute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(WiringError, match="cannot import wiring module"):
        load_wiring("definitely_not_a_module_xyz:build", _context(tmp_path))
    _install(monkeypatch, "wiring_fixture_missing", "present", object())
    with pytest.raises(WiringError, match="has no attribute"):
        load_wiring("wiring_fixture_missing:build", _context(tmp_path))


def test_non_callable_and_raising_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, "wiring_fixture_noncall", "build", 42)
    with pytest.raises(WiringError, match="non-callable"):
        load_wiring("wiring_fixture_noncall:build", _context(tmp_path))

    def boom(context: WiringContext) -> NoReturn:
        raise RuntimeError("factory exploded")

    _install(monkeypatch, "wiring_fixture_boom", "build", boom)
    with pytest.raises(WiringError, match="raised"):
        load_wiring("wiring_fixture_boom:build", _context(tmp_path))


def test_invalid_return_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, "wiring_fixture_bad", "build", _returns(object()))
    with pytest.raises(WiringError, match="lacks start/stop"):
        load_wiring("wiring_fixture_bad:build", _context(tmp_path))

    _install(monkeypatch, "wiring_fixture_mixed", "build", _returns([_Recorder(), "nope"]))
    with pytest.raises(WiringError, match="lacks start/stop"):
        load_wiring("wiring_fixture_mixed:build", _context(tmp_path))

    class _Never:
        async def start(self) -> None: ...
        async def stop(self) -> None: ...

    _install(monkeypatch, "wiring_fixture_class", "build", _returns(_Never))
    with pytest.raises(WiringError, match="class, not an instance"):
        load_wiring("wiring_fixture_class:build", _context(tmp_path))


def test_single_and_list_returns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    one = _Recorder()
    _install(monkeypatch, "wiring_fixture_single", "build", _returns(one))
    assert load_wiring("wiring_fixture_single:build", _context(tmp_path)) == (one,)

    two = (_Recorder(), _Recorder())
    _install(monkeypatch, "wiring_fixture_list", "build", _returns(list(two)))
    assert load_wiring("wiring_fixture_list:build", _context(tmp_path)) == two


async def test_stops_run_in_reverse_start_order() -> None:
    order: list[str] = []

    class _Participant:
        def __init__(self, name: str) -> None:
            self._name = name

        async def start(self) -> None:
            order.append(f"start:{self._name}")

        async def stop(self) -> None:
            order.append(f"stop:{self._name}")

    started = await start_participants([_Participant("a"), _Participant("b"), _Participant("c")])
    await stop_participants(started)
    assert order == ["start:a", "start:b", "start:c", "stop:c", "stop:b", "stop:a"]


async def test_failing_start_stops_the_already_started() -> None:
    first = _Recorder()

    class _Boom:
        async def start(self) -> None:
            raise RuntimeError("nope")

        async def stop(self) -> None: ...

    with pytest.raises(WiringError, match="failed to start"):
        await start_participants([first, _Boom()])
    assert first.events == ["start", "stop"]


async def test_stop_failure_is_logged_not_raised() -> None:
    class _BadStop:
        async def start(self) -> None: ...

        async def stop(self) -> None:
            raise RuntimeError("sad")

    await stop_participants([_BadStop()])

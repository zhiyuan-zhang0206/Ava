from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import pytest

from base.host import runtime_interpreter
from services.agent_runner.agent_host import daemon


def _make_plugin(root: Path, name: str, body: str = "x = 1\n") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.py").write_text(body, encoding="utf-8")


def test_fingerprint_empty_when_no_plugins_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime_interpreter, "external_plugin_read_root", lambda: tmp_path / "nope")
    assert daemon.plugins_fingerprint() == ""


def test_fingerprint_tracks_plugin_add_change_remove(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime_interpreter, "external_plugin_read_root", lambda: tmp_path)

    assert daemon.plugins_fingerprint() == ""
    _make_plugin(tmp_path, "alpha")
    f1 = daemon.plugins_fingerprint()
    assert "alpha" in f1

    _make_plugin(tmp_path, "beta")
    f2 = daemon.plugins_fingerprint()
    assert f1 != f2 and "beta" in f2

    (tmp_path / "notes.txt").write_text("hi", encoding="utf-8")
    assert daemon.plugins_fingerprint() == f2

    _make_plugin(tmp_path, "alpha", body="x = 2\n")
    f3 = daemon.plugins_fingerprint()
    assert f3 != f2

    import shutil as _shutil

    _shutil.rmtree(tmp_path / "beta")
    f4 = daemon.plugins_fingerprint()
    assert f4 != f3 and "beta" not in f4


@pytest.mark.asyncio
async def test_watch_restarts_on_plugin_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime_interpreter, "external_plugin_read_root", lambda: tmp_path)
    monkeypatch.setattr(daemon, "PLUGINS_POLL_INTERVAL_S", 0.01)

    raised: list[signal.Signals] = []

    def _raise(sig: signal.Signals) -> None:
        raised.append(sig)

    monkeypatch.setattr(daemon.signal, "raise_signal", _raise)

    watcher = asyncio.create_task(daemon.watch_plugins_for_restart())
    await asyncio.sleep(0.05)
    assert raised == []

    _make_plugin(tmp_path, "alpha")
    await asyncio.sleep(0.05)
    assert raised == [signal.SIGTERM]

    await watcher

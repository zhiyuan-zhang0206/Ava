"""`services.agent_host.daemon` plugin-discovery watchdog (issue #170).

The host loads external plugins exactly once per process, so a plugin
installed after boot is invisible until a restart. `_plugins_fingerprint`
and `_watch_plugins_for_restart` implement the ergonomic restart: on any
change under $AVA_HOME/plugins the daemon SIGTERMs itself and the runner
supervisor (watchdog -> healthcheck) brings it back with the new plugin
loaded — a restart, not a reload (S4 dispose is unimplemented).
"""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import pytest

from services.agent_host import daemon
from shared import runtime_interpreter


def _make_plugin(root: Path, name: str, body: str = "x = 1\n") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.py").write_text(body, encoding="utf-8")


def test_fingerprint_empty_when_no_plugins_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing plugins dir is a valid no-plugins state — empty, not an error."""
    monkeypatch.setattr(runtime_interpreter, "external_plugin_read_root", lambda: tmp_path / "nope")
    assert daemon._plugins_fingerprint() == ""


def test_fingerprint_tracks_plugin_add_change_remove(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fingerprint changes when a plugin dir is added, its plugin.py changes,
    or it is removed; unrelated files do not affect it."""
    monkeypatch.setattr(runtime_interpreter, "external_plugin_read_root", lambda: tmp_path)

    assert daemon._plugins_fingerprint() == ""
    _make_plugin(tmp_path, "alpha")
    f1 = daemon._plugins_fingerprint()
    assert "alpha" in f1

    _make_plugin(tmp_path, "beta")
    f2 = daemon._plugins_fingerprint()
    assert f1 != f2 and "beta" in f2

    # touching a non-plugin file does not change the fingerprint
    (tmp_path / "notes.txt").write_text("hi", encoding="utf-8")
    assert daemon._plugins_fingerprint() == f2

    # editing plugin.py changes it
    _make_plugin(tmp_path, "alpha", body="x = 2\n")
    f3 = daemon._plugins_fingerprint()
    assert f3 != f2

    # removing a plugin changes it (dir gone entirely, not renamed)
    import shutil as _shutil

    _shutil.rmtree(tmp_path / "beta")
    f4 = daemon._plugins_fingerprint()
    assert f4 != f3 and "beta" not in f4


@pytest.mark.asyncio
async def test_watch_restarts_on_plugin_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fingerprint change makes the watcher SIGTERM itself once, then stop —
    the supervisor restarts the host, which loads the new plugin at boot."""
    monkeypatch.setattr(runtime_interpreter, "external_plugin_read_root", lambda: tmp_path)
    monkeypatch.setattr(daemon, "_PLUGINS_POLL_INTERVAL_S", 0.01)

    raised: list[signal.Signals] = []

    def _raise(sig: signal.Signals) -> None:
        raised.append(sig)

    monkeypatch.setattr(daemon.signal, "raise_signal", _raise)

    watcher = asyncio.create_task(daemon._watch_plugins_for_restart())
    await asyncio.sleep(0.05)  # first poll: empty == empty, no kill
    assert raised == []

    _make_plugin(tmp_path, "alpha")
    await asyncio.sleep(0.05)
    assert raised == [signal.SIGTERM]

    await watcher  # the watcher returns after the kill

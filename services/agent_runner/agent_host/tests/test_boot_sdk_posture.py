"""The native daemon fixes SDK process posture before any boot dependency read."""

from __future__ import annotations

import pytest

import ava
from base import config
from services.agent_runner.agent_host import daemon


def test_sdk_posture_precedes_config_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    class BootStoppedError(RuntimeError):
        pass

    reads: list[str] = []

    def fix_posture() -> None:
        reads.append("sdk_posture")
        raise BootStoppedError("stop before boot")

    monkeypatch.setattr(ava, "bind_host_process", fix_posture)
    monkeypatch.setattr(config, "ensure_eager", lambda: reads.append("config"))
    with pytest.raises(BootStoppedError, match="before boot"):
        daemon.main()
    assert reads == ["sdk_posture"]

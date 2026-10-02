"""Where this home keeps its native Redis, and how it names its Redis port.

The root diagnostics and the cli bring-up/stop read the same facts here; a drift between
them makes a diagnostic look at a different directory than the process it observes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from base.cluster import ownership
from base.config import settings


def test_redis_data_dir_is_redis_under_the_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    assert ownership.redis_data_dir() == tmp_path / "redis"


@pytest.mark.parametrize(
    ("url", "port"),
    [
        ("redis://127.0.0.1:6380/0", 6380),
        ("redis://:secret@10.0.0.9:6391/2", 6391),
        ("redis://127.0.0.1/0", None),
    ],
)
def test_configured_redis_port_reads_only_the_url_port(
    monkeypatch: pytest.MonkeyPatch, url: str, port: int | None
) -> None:
    monkeypatch.setattr(settings.data_plane, "redis_url", url)
    assert ownership.configured_redis_port() == port

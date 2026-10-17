from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from base.config import ConfigBoot


@pytest.fixture(autouse=True)
def _isolated_owner_environment(tmp_path: Path) -> Iterator[None]:
    with patch.dict(os.environ):
        os.environ["MEIVA_HOME"] = str(tmp_path)
        (tmp_path / ".env").write_text(
            "MEIVA_MACHINE_SERVE_GATEWAY=true\n"
            "MEIVA_DB_URL=postgresql://test:test@127.0.0.1:1/initial\n"
            "MEIVA_REDIS_URL=redis://127.0.0.1:1/0\n",
            encoding="utf-8",
        )
        yield


def test_gateway_excludes_walg_while_cli_owner_supplies_its_configured_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.backup.walg.config import configured_path

    monkeypatch.delenv("MEIVA_PROCESS_PROFILE", raising=False)
    cli = ConfigBoot()
    path = tmp_path / "walg.json"
    cli.set_field("walg_config_file", path)

    def path_reader() -> Path | None:
        return cli.view.walg.walg_config_file

    assert cli.view.has_domain("walg")
    assert configured_path(path_reader=path_reader) == path
    monkeypatch.setenv("MEIVA_PROCESS_PROFILE", "gateway")
    gateway = ConfigBoot()
    assert not gateway.view.has_domain("walg")
    with pytest.raises(AttributeError, match="profile"):
        _ = gateway.view.walg
    gateway.ensure_eager()
    assert not gateway.view.has_domain("walg")
    with pytest.raises(AttributeError, match="profile"):
        _ = gateway.view.walg
    assert configured_path(path_reader=path_reader) == path

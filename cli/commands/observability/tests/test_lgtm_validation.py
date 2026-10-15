from __future__ import annotations

from pathlib import Path

import pytest

from base.host.system.tests.fake_commands import FakeCommands, Reply, fake_host
from cli.commands.observability import lgtm_native


def test_pinned_loki_parser_rejection_blocks_preparation(tmp_path: Path) -> None:
    commands = FakeCommands()
    commands.on([], Reply(returncode=1, stderr="unknown config field"))
    with pytest.raises(RuntimeError, match="unknown config field"):
        lgtm_native.verify_loki(tmp_path, host=fake_host(commands))
    assert commands.argvs == [
        (
            str(tmp_path / "lgtm/native/bin/loki"),
            f"-config.file={tmp_path}/lgtm/native/config/loki.yaml",
            "-verify-config",
        )
    ]
    assert commands.calls[0].timeout == 30

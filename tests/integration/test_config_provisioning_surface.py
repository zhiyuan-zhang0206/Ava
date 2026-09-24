"""The operator provisioning surface for the manifest certification secret (#4719).

Lives in its own module — not in `test_config_cmd.py`, which is pinned at its
structure budget (804 lines) — so this surface proof cannot be blocked by that
budget again.

The certification secret must be provisioned on every participating
agent-runner through the supported local config path (direct `.env` writes are
barred by the 2026-09-01 ruling), must never be writable remotely (the value
must not traverse the gateway), and must stay masked on reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.commands import config as cfg
from shared import runtime_config
from shared.api_contracts.config import ConfigFieldView

_KEY = "AVA_IMPERSONATION_EVENT_MANIFEST_CERTIFICATION_SECRET"


@pytest.fixture()
def local_env_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(runtime_config, "_ava_home", lambda: tmp_path)
    return tmp_path


def test_manifest_certification_secret_provisioning_surface(
    local_env_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """All three proofs for the field: local write allowed, remote write
    rejected, masking preserved (task #4719)."""
    from shared.config import get_config_metadata

    secret = "ab" * 32  # 64 chars; the field requires >= 32
    (local_env_home / ".env").write_text("OTHER=kept\n")

    # (1) local operator write = allowed: the supported --local path lands the
    # value in this unit's .env and keeps unrelated lines.
    rc = cfg.cmd_config_set([f"{_KEY}={secret}"], machine=None, local=True)
    assert rc == 0
    aliases = runtime_config.read_env_aliases()
    assert aliases[_KEY] == secret
    assert "OTHER" in aliases

    # (3) sensitive masking preserved: the local read prints bullets, never the value.
    capsys.readouterr()
    rc = cfg.cmd_config_get(_KEY, machine=None, local=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022" in out
    assert secret not in out

    # (2) remote write = rejected: the machine-addressed gate refuses this
    # field's declared shape (remote_writable=False) — the value must never
    # traverse the gateway.
    meta = next(
        item
        for item in get_config_metadata()
        if item.name == "impersonation_event_manifest_certification_secret"
    )
    assert meta.scope == "host"
    assert meta.sensitive is True
    assert meta.remote_writable is False
    view = ConfigFieldView(
        name=meta.name,
        field_type="string",
        current_value=None,
        default_value=None,
        description="",
        group="",
        capability="common",
        restart_required="all",
        writable=meta.writable,
        sensitive=meta.sensitive,
        env_var=meta.env_var,
        scope=meta.scope,
        remote_writable=meta.remote_writable,
        per_agent=False,
    )
    assert cfg._field_editable(view, remote=True) is False
    assert cfg._field_editable(view, remote=False) is True

"""Fixtures for impersonation tests."""

from typing import Any

import pytest

from base.agents import impersonation as leases
from base.native_process import native_boot_id


def request_legacy_leases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make new automatic leases carry no event protocol (the pre-manifest, replay-read kind)."""

    def no_protocol(*, automatic: bool) -> None:
        del automatic

    monkeypatch.setattr(leases, "event_protocol_for_new_lease", no_protocol)


def native_identity(birth: float) -> dict[str, object]:
    """Explicit synthetic process evidence within this test host's boot scope."""
    import sys

    return {
        "created_at": birth,
        "starttime": int(birth * 100) if sys.platform == "linux" else None,
        "boot_id": native_boot_id(),
    }


def recorded_tree() -> dict[str, Any]:
    """A recorded controller process tree."""
    return {
        "pid": 4242,
        "name": "python3.12",
        "executable": "/usr/bin/python3.12",
        **native_identity(1000.0),
        "parent_pid": 4241,
        "ancestors": [
            {
                "pid": 4241,
                "name": "zsh",
                "executable": "/bin/zsh",
                **native_identity(999.0),
                "parent_pid": 4240,
            },
            {
                "pid": 4240,
                "name": "codex",
                "executable": "/opt/codex",
                **native_identity(998.0),
                "parent_pid": 1,
            },
        ],
    }

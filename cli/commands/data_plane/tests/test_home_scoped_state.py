"""Host-level state lives in the home.

The host runs one cluster, so there is no separate host state directory: the
vendored Postgres runtime, the initdb template, the coding-session owner records
and the PTY allocation freeze sit under `$AVA_HOME`, and a test that points
`AVA_HOME` at a tmp dir has isolated all of them with nothing else to redirect.
"""

from __future__ import annotations

from pathlib import Path

from base.cluster.dataplane import runtime_binaries
from base.config import settings
from base.sessions import coding_session_owner_record
from base.sessions.pty import allocation_freeze
from cli.commands.data_plane import cluster_instance


def test_every_host_state_path_follows_ava_home(unit_home: Path) -> None:
    assert runtime_binaries.runtime_root() == unit_home / "runtime"
    assert cluster_instance._pg_template_dir() == unit_home / "pg-template-17"
    assert coding_session_owner_record._host_owner_dir() == unit_home / "coding-session-owners"
    assert allocation_freeze.state_path() == unit_home / "pty-allocation-freeze.json"
    assert allocation_freeze.lock_path() == unit_home / "pty-allocation.lock"


def test_there_is_no_host_state_dir_setting_or_helper() -> None:
    import base.paths

    assert "host_state_dir" not in type(settings.general).model_fields
    assert not hasattr(base.paths, "host_state_dir")

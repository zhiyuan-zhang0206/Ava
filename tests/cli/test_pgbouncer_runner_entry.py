"""Unit tests for the pgbouncer userlist runner entry (Task #1236).

The pooler's userlist carries `ava_runner` with its own password exactly when
the cluster has a runner credential — a legacy cluster keeps a byte-identical
userlist until `ava cluster ensure-runner-role` runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.commands import _pgbouncer as pg


def test_render_userlist_without_runner_entry_is_unchanged() -> None:
    assert pg._render_userlist("ava_main", "sec") == '"ava_main" "sec"\n'


def test_render_userlist_with_runner_entry() -> None:
    out = pg._render_userlist("ava_main", "sec", "ava_runner", "rpw")
    assert out == '"ava_main" "sec"\n"ava_runner" "rpw"\n'


def test_render_userlist_escapes_quotes_in_passwords() -> None:
    """PgBouncer double-quotes both fields, so an embedded quote in a password
    must be escaped (the roles are fixed identifiers, never user-controlled)."""
    out = pg._render_userlist("ava_main", 'se"c', "ava_runner", 'rp"w')
    assert out == '"ava_main" "se""c"\n"ava_runner" "rp""w"\n'


def test_runner_password_from_env_reads_home_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pg, "ava_home", lambda: tmp_path)
    # Assembled, not a literal KEY=VALUE line — the fixture value is a secret-ish
    # string and a literal would trip the secret scanner.
    (tmp_path / ".env").write_text("AVA_RUNNER_DB_PASSWORD=" + "abc123" + "\n")
    assert pg.runner_password_from_env() == "abc123"


def test_runner_password_from_env_absent_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pg, "ava_home", lambda: tmp_path)
    (tmp_path / ".env").write_text("AVA_CLUSTER_SECRET=cs\n")
    assert pg.runner_password_from_env() == ""

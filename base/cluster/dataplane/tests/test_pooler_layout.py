"""Where this home keeps its PgBouncer files.

The cli bring-up/stop and the root diagnostics read the same two paths; a drift between
them makes a diagnostic look at a different pooler than the process it observes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from base.cluster.dataplane import pooler


def test_pooler_files_live_in_the_pgbouncer_directory_under_the_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    assert not (tmp_path / "pgbouncer").exists()
    assert pooler.ini_path() == tmp_path / "pgbouncer" / "pgbouncer.ini"
    assert pooler.pidfile_path() == tmp_path / "pgbouncer" / "pgbouncer.pid"
    assert (tmp_path / "pgbouncer").is_dir()

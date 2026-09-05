"""Failed throwaway-cluster starts preserve pg.log for CI artifacts (#1037).

The throwaway cluster lives on a tmpfs (/dev/shm on Linux, $TMPDIR on mac)
and `throwaway_postgres` rmtrees the instance dir on teardown, so when
`pg_ctl start` refuses, the reason is unrecoverable after the fact. The fix
copies the instance's pg.log into an artifact dir before the teardown
rmtree, and the raised error carries the log tail so the CI log itself
shows why the cluster failed to come up.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from shared import pg_tools
from shared.platform import IS_WINDOWS

pytestmark = pytest.mark.skipif(IS_WINDOWS, reason="throwaway clusters are POSIX-only")


@pytest.fixture
def scratch_tmpfs(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Pin the throwaway root at a short scratch dir. The Postgres socket
    path (`<dir>/.s.PGSQL.<port>`) is capped at 103 bytes, so the root must
    be short (same constraint as tests/shared/test_pg_throwaway_sweep.py).
    Teardown force-stops anything still running under the root and deletes
    it, so a failing test cannot leak a cluster."""
    root = Path(tempfile.mkdtemp(prefix="ava-pglog-", dir="/tmp"))
    monkeypatch.setattr(pg_tools, "_tmpfs_base", str(root))
    yield root
    for data in root.glob("ava-pg-*/data"):
        if (data / "PG_VERSION").is_file():
            subprocess.run(  # noqa: S603 — argv is the resolved pg_ctl path + this test's own dir
                [pg_tools.pg_tool("pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
                check=False,
                capture_output=True,
            )
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def artifact_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect preserved logs into this test's own dir. The real location is
    derived from the repo root, so patch the deriving function — writing into
    <repo>/tmp/ from a test would leave artifacts behind."""
    target = tmp_path / "artifacts"
    monkeypatch.setattr(pg_tools, "_fixture_log_artifact_dir", lambda: target)
    return target


def test_failed_start_preserves_pg_log_into_artifacts(
    scratch_tmpfs: Path, artifact_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """initdb succeeds, then `pg_ctl start` fails because the port is taken
    (the exact failure mode from the issue: 738 errors, one pg_ctl refusal).
    The pg.log must survive the teardown rmtree in the artifact dir, and the
    raised error must carry the log tail + artifact path."""
    # Occupy a port so the postmaster cannot bind it. initdb has already
    # succeeded by then, so the failure is purely in `pg_ctl start`.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        monkeypatch.setattr(pg_tools, "_free_port", lambda: blocker.getsockname()[1])
        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            # __enter__() runs the generator body — the bare call only builds it.
            pg_tools.throwaway_postgres().__enter__()
        # The raised error names the preserved artifact and shows the tail.
        assert "pg.log artifact" in str(excinfo.value)
        assert "Address already in use" in str(excinfo.value)
    # The instance dir itself is gone (teardown rmtree ran)…
    assert list(scratch_tmpfs.glob("ava-pg-*")) == []
    # …but its pg.log was copied out before that.
    saved = list(artifact_dir.glob("*.pg.log"))
    assert len(saved) == 1
    assert "Address already in use" in saved[0].read_text()


def test_failure_before_any_pg_log_still_writes_an_artifact(
    scratch_tmpfs: Path, artifact_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """postgres can fail before it ever opens the log `-l` names: here initdb
    itself refuses, so `pg_ctl start` never runs and no pg.log is created. The
    artifact must still be written, saying that — an absent artifact is
    indistinguishable from the capture being broken, which is the state #1037
    was reported from."""
    stub_initdb = tmp_path / "failing-initdb"
    stub_initdb.write_text("#!/bin/sh\necho 'initdb: error: synthetic failure' >&2\nexit 1\n")
    stub_initdb.chmod(0o755)
    real_pg_tool = pg_tools.pg_tool
    monkeypatch.setattr(
        pg_tools,
        "pg_tool",
        lambda name: stub_initdb if name == "initdb" else real_pg_tool(name),  # pyright: ignore[reportUnknownArgumentType]
    )

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        pg_tools.throwaway_postgres().__enter__()

    assert pg_tools._NO_PG_LOG_NOTE in str(excinfo.value)
    saved = list(artifact_dir.glob("*.pg.log"))
    assert len(saved) == 1
    assert pg_tools._NO_PG_LOG_NOTE in saved[0].read_text()


def test_artifact_dir_defaults_under_repo_tmp() -> None:
    """The location is derived, not configured — so this is the one place the
    path is pinned, and it must stay the literal path the CI upload-artifact
    step names (`tmp/pg-fixture-logs/` in .github/workflows/ci.yml and
    deploy/ci/public-ci.yml). Follows the e2e-logs convention."""
    from shared.paths import repo_root

    assert pg_tools._fixture_log_artifact_dir() == repo_root() / "tmp" / "pg-fixture-logs"

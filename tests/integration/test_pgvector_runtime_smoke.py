"""CI hard gate for the vendored-runtime pgvector injection.

Proves on the real pinned artifacts that the vendored relocatable Postgres PLUS
the injected pgvector extension files work end to end: `CREATE EXTENSION
vector` + a real distance query, plus the NOSUPERUSER semantics the runtime
depends on (the indexer's connect() issues `CREATE EXTENSION IF NOT EXISTS` as
the cluster's NOSUPERUSER role; with the extension pre-created by `ava start`'s
superuser connection that must be a harmless no-op, not a privilege error).

Runs on both supported platforms (macOS = Homebrew bottle, Linux = PGDG deb).
The Linux leg is the acceptance red line and runs as the dedicated CI job
`backend-pgvector-smoke`; the test file is excluded from the pytest shards so
the artifact downloads happen once per run. The other PG clusters in the suite
run on the apt/brew Postgres; this one runs on the vendored tree exclusively,
so a regression in the injection path cannot hide behind the host install.
"""

from __future__ import annotations

import platform
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest

from shared import runtime_binaries as rb
from shared.config import settings
from shared.pg_tools import throwaway_postgres


@pytest.fixture()
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the host-level runtime root at a tmp dir (via the cluster-registry
    anchor), so the test never touches the real ~/.ava/runtime."""
    monkeypatch.setattr(settings.general, "cluster_registry", str(tmp_path / "clusters.json"))


def _platform_supported() -> bool:
    system = platform.system()
    if system == "Darwin":
        return platform.machine() in ("arm64", "x86_64", "amd64")
    if system == "Linux":
        return platform.machine() in ("x86_64", "amd64")
    return False


def test_vendored_pg_with_injected_pgvector_creates_extension_and_queries(
    isolated_runtime: None,
) -> None:
    if not _platform_supported():
        pytest.skip("no pgvector artifact pinned for this platform (linux/arm64 is out of matrix)")
    bin_dir = rb.ensure_pg_binaries()
    rb.ensure_pgvector()
    injected = rb.vendored_pg_dir() / "share/postgresql/extension"
    assert (injected / rb._PGVECTOR_SQL).exists()

    # The throwaway cluster resolves initdb/pg_ctl via pg_tool, which prefers
    # the vendored tree just built — so this exercises the vendored binaries,
    # not the host's brew/apt install.
    with throwaway_postgres() as url:
        port = urlsplit(url).port
        with psycopg.connect(url) as conn:
            conn.execute("CREATE EXTENSION vector")
            conn.execute("CREATE TABLE smoke_vectors (v vector(3))")
            conn.execute("INSERT INTO smoke_vectors VALUES ('[1,2,3]'), ('[4,5,6]')")
            rows = conn.execute(
                "SELECT v <-> '[1,2,4]'::vector FROM smoke_vectors ORDER BY 1"
            ).fetchall()
            assert len(rows) == 2
            # L2: [1,2,3] vs [1,2,4] = 1.0; [4,5,6] vs [1,2,4] = sqrt(9+9+4).
            assert abs(float(rows[0][0]) - 1.0) < 1e-6
            assert abs(float(rows[1][0]) - (9 + 9 + 4) ** 0.5) < 1e-6
            # Cosine on the injected extension too.
            cosine = conn.execute("SELECT '[1,0,0]'::vector <=> '[1,0,0]'::vector").fetchone()
            assert cosine is not None and abs(float(cosine[0])) < 1e-6
            # NOSUPERUSER semantics the runtime relies on: the extension shows up
            # in pg_available_extensions for a non-superuser, and with it already
            # present (pre-created by `ava start`'s superuser connection) the
            # indexer's NOSUPERUSER `CREATE EXTENSION IF NOT EXISTS` is a no-op.
            conn.execute("CREATE ROLE smoke_nosuper LOGIN NOSUPERUSER")
        with psycopg.connect(f"postgresql://smoke_nosuper@127.0.0.1:{port}/ava_citest") as conn2:
            row = conn2.execute(
                "SELECT count(*) FROM pg_available_extensions WHERE name = 'vector'"
            ).fetchone()
            assert row is not None and int(row[0]) == 1
            conn2.execute("CREATE EXTENSION IF NOT EXISTS vector")
        # The bin dir is a fresh throwaway on the vendored tree — pg_tool prefers
        # it once it exists.
        assert rb.vendored_pg_bin_dir() == bin_dir

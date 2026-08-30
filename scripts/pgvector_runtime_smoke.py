#!/usr/bin/env python
"""Vendored-runtime pgvector smoke — the CI hard gate (CTO acceptance red line).

Proves on the real pinned artifacts that the vendored relocatable Postgres PLUS
the injected pgvector extension files work end to end, with no host Postgres:
download the pinned zonky PG (ensure_pg_binaries) -> inject the pinned pgvector
files (ensure_pgvector) -> start a throwaway cluster on the vendored binaries ->
CREATE EXTENSION vector + distance queries -> NOSUPERUSER semantics (the
indexer's connect() issues CREATE EXTENSION IF NOT EXISTS as the cluster's
NOSUPERUSER role; with the extension pre-created by `ava start`'s superuser
connection that must be a harmless no-op, not a privilege error — verified
empirically on a real injected tree).

Runs as the dedicated CI job `backend-pgvector-smoke` on Linux x86_64 (the
acceptance evidence for the PGDG deb leg) and locally on supported hosts
(macOS = Homebrew bottle leg). Deliberately a script, not a pytest test: the
test suite's session fixture provisions its own Postgres via pg_tool, so a
vendored-only CI job cannot run pytest at all.

Exit 0 = the injected tree served CREATE EXTENSION + queries; anything else
fails the job.
"""

from __future__ import annotations

import platform
import sys
from urllib.parse import urlsplit

import psycopg

from shared import runtime_binaries as rb
from shared.pg_tools import throwaway_postgres


def _assert_platform_supported() -> None:
    system = platform.system()
    machine = platform.machine()
    supported = (system == "Darwin" and machine in ("arm64", "x86_64", "amd64")) or (
        system == "Linux" and machine in ("x86_64", "amd64")
    )
    if not supported:
        raise RuntimeError(
            f"no pinned pgvector artifact for {system}/{machine} (linux/arm64 is out of matrix)"
        )


def main() -> int:
    _assert_platform_supported()
    bin_dir = rb.ensure_pg_binaries()
    rb.ensure_pgvector()

    pg_dir = rb.vendored_pg_dir()
    injected_sql = next((pg_dir / "share/postgresql/extension").glob("vector--*.sql"), None)
    if injected_sql is None:
        raise RuntimeError(f"pgvector injection incomplete at {pg_dir}/share/postgresql/extension")
    if rb.vendored_pg_bin_dir() != bin_dir:
        raise RuntimeError(f"vendored bin dir resolution drifted: {rb.vendored_pg_bin_dir()}")

    # The throwaway cluster resolves initdb/pg_ctl via pg_tool, which prefers
    # the vendored tree just built — so everything below runs on the vendored
    # binaries, not any host install.
    with throwaway_postgres() as url:
        port = urlsplit(url).port
        with psycopg.connect(url) as conn:
            conn.execute("CREATE EXTENSION vector")
            conn.execute("CREATE TABLE smoke_vectors (v vector(3))")
            conn.execute("INSERT INTO smoke_vectors VALUES ('[1,2,3]'), ('[4,5,6]')")
            rows = conn.execute(
                "SELECT v <-> '[1,2,4]'::vector FROM smoke_vectors ORDER BY 1"
            ).fetchall()
            if len(rows) != 2:
                raise RuntimeError(f"L2 query returned {len(rows)} rows, expected 2")
            # L2: [1,2,3] vs [1,2,4] = 1.0; [4,5,6] vs [1,2,4] = sqrt(9+9+4).
            if abs(float(rows[0][0]) - 1.0) >= 1e-6:
                raise RuntimeError(f"L2 distance mismatch on row 1: {rows[0]}")
            if abs(float(rows[1][0]) - (9 + 9 + 4) ** 0.5) >= 1e-6:
                raise RuntimeError(f"L2 distance mismatch on row 2: {rows[1]}")
            cosine = conn.execute("SELECT '[1,0,0]'::vector <=> '[1,0,0]'::vector").fetchone()
            if cosine is None or abs(float(cosine[0])) >= 1e-6:
                raise RuntimeError(f"cosine distance mismatch: {cosine}")
            # NOSUPERUSER semantics the runtime relies on (see module docstring).
            conn.execute("CREATE ROLE smoke_nosuper LOGIN NOSUPERUSER")
        with psycopg.connect(f"postgresql://smoke_nosuper@127.0.0.1:{port}/ava_citest") as conn:
            row = conn.execute(
                "SELECT count(*) FROM pg_available_extensions WHERE name = 'vector'"
            ).fetchone()
            if row is None or int(row[0]) != 1:
                raise RuntimeError(
                    "pg_available_extensions does not list 'vector' for a NOSUPERUSER role"
                )
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")

    return 0


if __name__ == "__main__":
    sys.exit(main())

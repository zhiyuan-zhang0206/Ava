"""A session-scoped standalone milvus-lite server for the memory-index tests.

Opt-in: only tests that take `milvus_client` (or `milvus_server`) pay the ~3s
spawn, once per pytest process.
"""

import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def milvus_server() -> Iterator[str]:
    """Session-scoped standalone milvus-lite server.

    Spawns `milvus-lite server --data-dir <tmp> --port <random>` once per pytest
    session, shared by all memory_indexer / ava.memory tests. URI exposed via fixture,
    tests use `monkeypatch.setenv("AVA_MILVUS_URI", ...)` to let `index.connect()` connect
    to this subprocess instead of prod default 19530.

    Consistent with prod using standalone server —— not mixing lite-in-process, avoiding behavior drift.
    Spawn cost ~3s once per session; tests not referencing this fixture (e.g. fast subset)
    don't trigger spawn.
    """
    import shutil
    import socket
    import subprocess
    import sys
    import tempfile
    import time

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    tmpdir = Path(tempfile.mkdtemp(prefix=f"ava_milvus_test_{port}_"))
    # `.venv/bin/milvus-lite` —— Python sys.executable is in .venv/bin/python, binary in same dir.
    # Use absolute path not dependent on whether caller uses `uv run` (PATH might not have .venv/bin/).
    binary = Path(sys.executable).parent / "milvus-lite"
    proc = subprocess.Popen(  # noqa: S603 — binary is neighbor of sys.executable, args hardcoded
        [
            str(binary),
            "server",
            "--data-dir",
            str(tmpdir),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    uri = f"http://127.0.0.1:{port}"
    # Wait for server listen —— TCP probe up to 10s, milvus-lite startup needs 1-3s.
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError(f"milvus-lite server failed to start on :{port} within 10s")

    yield uri

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(tmpdir, ignore_errors=True)


_MILVUS_TEST_DIM = 8
"""Test vector width — small, matches the embedding dummy vectors."""
_MILVUS_TEST_FP = "test:gemini:dim=8"
"""Test provider fingerprint — stamped on rows, compared by reconcile tests."""


@pytest.fixture
def milvus_client(milvus_server: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """Fresh MilvusClient connected to session server, after test drop collection for isolation.

    Each test gets a clean collection —— `MilvusBackend(dim=..., fingerprint=...)`'
    connect() idempotently creates it, yields the underlying client,
    teardown drops so next test sees empty collection.
    """
    # Settings module-loaded once BaseSettings, setenv then settings.services.milvus_uri
    # does not re-read env. Directly monkeypatch.setattr change Settings instance field, consistent with
    # ava/tests/test_web.py and other monkeypatch patterns.
    from base.config import settings

    monkeypatch.setattr(settings.services, "milvus_uri", milvus_server)
    from services.memory_indexer.backends.milvus import _COLLECTION, MilvusBackend

    backend = MilvusBackend(dim=_MILVUS_TEST_DIM, fingerprint=_MILVUS_TEST_FP)
    backend.connect()
    client = backend._require_client()
    try:
        yield client
    finally:
        with contextlib.suppress(Exception):
            # pyright infers pymilvus client.has_collection as coroutine (stub noise),
            # actually sync bool. Keep suppress to avoid reportUnnecessaryComparison false positive.
            if client.has_collection(_COLLECTION):  # pyright: ignore[reportUnnecessaryComparison]
                client.drop_collection(_COLLECTION)
        with contextlib.suppress(Exception):
            client.close()

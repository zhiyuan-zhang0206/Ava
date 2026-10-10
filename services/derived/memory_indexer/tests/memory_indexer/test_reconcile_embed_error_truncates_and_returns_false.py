"""Memory indexer cases: reconcile embed error truncates and returns false."""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
import pytest
from pydantic import SecretStr

from base.config import ConfigBoot
from base.daemon.health import Liveness
from services.derived.memory_indexer import daemon
from services.derived.memory_indexer.backends.base import content_hash
from services.derived.memory_indexer.embeddings.base import EmbeddingAPIError
from services.derived.memory_indexer.tests.store_backend import StoreBackend
from services.derived.memory_indexer.tests.test_memory_indexer import (
    _DIM,
    _FP,
    _assert_gap_closed,
    _assert_one_ladder_rung,
    _assert_retry_idle,
    _cancel_quietly,
    _FakeProvider,
    _FlakyProvider,
    _liveness_timeout,
    _long_note,
)
from services.derived.memory_indexer.tests.test_memory_indexer import (
    _watched_root as _watched_root,
)
from services.derived.memory_indexer.tests.test_memory_indexer import (
    store_backend as store_backend,
)


@pytest.fixture
def owned_config_environment() -> Iterator[None]:
    """Restore environment delivery and the process timezone after a real ConfigBoot."""
    try:
        with patch.dict(os.environ):
            yield
    finally:
        tzset = getattr(time, "tzset", None)
        if tzset is not None:
            tzset()


def test_reconcile_embed_error_truncates_and_returns_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Error semantics preserved from the pre-chunking reconcile (one
    try/except): an EmbeddingAPIError skips the remaining chunks and is logged
    with its error repr — never raised — so the daemon keeps running and
    the return value signals that a follow-up pass is needed."""
    root = tmp_path / "watched"
    root.mkdir()
    for i in range(6):
        (root / f"note-{i}.md").write_text(f"content {i}")
    monkeypatch.setattr(daemon, "_memory_root", lambda: root)
    monkeypatch.setattr(daemon, "_RECONCILE_CHUNK_PATHS", 2)  # 6 files -> 3 chunks

    calls: list[int] = []

    def failing_process_paths(
        backend: Any, paths: set[Path], provider: Any, liveness: Liveness
    ) -> None:
        calls.append(len(paths))
        raise EmbeddingAPIError("embed boom")

    monkeypatch.setattr(daemon, "_process_paths", failing_process_paths)

    liveness = Liveness(600.0)
    with caplog.at_level(logging.ERROR, logger="services.derived.memory_indexer.daemon"):
        assert daemon._reconcile(store_backend, _FakeProvider(), liveness) is False

    assert calls == [2]  # first chunk failed -> remaining chunks never ran (as before)
    expected = (
        f"reconcile embed failed: {EmbeddingAPIError('embed boom')!r}"
        " — daemon continues; a follow-up pass will retry"
    )
    assert expected in caplog.text
    assert liveness.is_alive()


def test_refresh_gateway_checkout_fast_forwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Safety net: pulls origin/main into the gateway checkout and logs
    loudly when HEAD moved (a post-merge refresh was missed)."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603 - fixed argv, test sandbox
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)  # noqa: S603 - fixed argv, test sandbox
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)  # noqa: S603 - fixed argv, test sandbox
    (tmp_path / "a.md").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)  # noqa: S603 - fixed argv, test sandbox
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)  # noqa: S603 - fixed argv, test sandbox
    before = subprocess.check_output(  # noqa: S603 — fixed argv, test sandbox  # noqa: S603 - fixed argv, test sandbox
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()

    from base.deploy.git import memory_repo

    monkeypatch.setattr(memory_repo, "gateway_memory_dir", lambda: tmp_path)
    monkeypatch.setattr(memory_repo, "pull_main", lambda: "abc1234")

    with caplog.at_level(logging.INFO, logger="services.derived.memory_indexer.daemon"):
        daemon._refresh_gateway_checkout()
    assert "fast-forwarded" in caplog.text
    assert before != "abc1234"


def test_refresh_gateway_checkout_failure_logs_and_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed pull is logged at ERROR and never raised — the drain loop
    retries next cycle instead of letting the daemon die."""
    from base.deploy.git import memory_repo

    monkeypatch.setattr(memory_repo, "gateway_memory_dir", lambda: tmp_path)

    def _boom() -> str:
        raise RuntimeError("network down")

    monkeypatch.setattr(memory_repo, "pull_main", _boom)

    with caplog.at_level(logging.ERROR, logger="services.derived.memory_indexer.daemon"):
        daemon._refresh_gateway_checkout()  # must not raise
    assert "refresh failed" in caplog.text


def test_split_note_extracts_description_and_body() -> None:
    content = (
        "---\ntype: Memory\ndescription: A note about the user's health\n"
        "---\n\n# Health\n\nbody content"
    )
    desc, body = daemon._split_note(content)
    assert desc == "A note about the user's health"
    assert "# Health" in body
    assert "description" not in body


def test_split_note_no_frontmatter_returns_full_body() -> None:
    content = "# Just a heading\n\nNo YAML."
    desc, body = daemon._split_note(content)
    assert desc is None
    assert body == content


def test_split_note_blank_description_is_none() -> None:
    desc, body = daemon._split_note("---\ndescription: \n---\n\nbody")
    assert desc is None
    assert body == "\nbody"  # the shared parser keeps the blank line after the fence


def test_chunk_body_short_text_single_chunk() -> None:
    assert daemon._chunk_body("short body") == ["short body"]
    assert daemon._chunk_body("  \n\n  ") == []


def test_chunk_body_splits_at_paragraph_boundaries() -> None:
    paras = [f"paragraph-{i} " + "word " * 80 for i in range(6)]  # 412 chars each
    body = "\n\n".join(paras)
    chunks = daemon._chunk_body(body, max_chars=1800, overlap_chars=200)
    assert len(chunks) == 2
    assert all(len(c) <= 1800 for c in chunks)
    assert chunks[0].startswith("paragraph-0")
    assert chunks[1].startswith("paragraph-4")  # 4×412 = 1654 fits; 5×412 would not
    # every paragraph survives whole (chunking strips paragraph whitespace)
    assert all(p.strip() in "".join(chunks) for p in paras)


def test_chunk_body_overlap_carries_trailing_paragraphs() -> None:
    paras = [f"paragraph-{i} " + "word " * 80 for i in range(6)]
    body = "\n\n".join(paras)
    chunks = daemon._chunk_body(body, max_chars=1400, overlap_chars=600)
    # the previous chunk's tail paragraph re-opens the next chunk
    assert chunks[1].startswith("paragraph-2")
    assert "paragraph-2" in chunks[0]


def test_chunk_body_hard_splits_oversized_paragraph() -> None:
    para = "x" * 3000
    chunks = daemon._chunk_body(para, max_chars=1000, overlap_chars=100)
    assert chunks == ["x" * 1000, "x" * 1000, "x" * 1000, "x" * 300]


def test_file_rows_desc_plus_body_chunks() -> None:
    content = "---\ntype: Memory\ndescription: hand off to 402\n---\n\n" + "\n\n".join(
        f"paragraph-{i} " + "word " * 80 for i in range(6)
    )
    rows = daemon._file_rows(content)
    assert rows[0] == ("desc", 0, "hand off to 402")
    assert [k for k, _, _ in rows] == ["desc", "body", "body"]


def test_file_rows_no_frontmatter_no_desc() -> None:
    assert daemon._file_rows("# heading\n\nshort body") == [("body", 0, "# heading\n\nshort body")]


def test_process_paths_indexes_desc_and_body_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
) -> None:
    """One file with a description + a long body lands as 1 desc row + N body
    chunks; all_meta still reports the file once."""
    f = tmp_path / "long.md"
    f.write_text(
        "---\ntype: Memory\ndescription: hand off to 402\n---\n\n"
        + "\n\n".join(f"paragraph-{i} " + "word " * 80 for i in range(6)),
        encoding="utf-8",
    )

    daemon._process_paths(
        store_backend,
        {f.resolve()},
        _FakeProvider(),
        Liveness(_liveness_timeout()),
    )
    assert str(f.resolve()) in store_backend.all_meta()
    kinds = sorted(store_backend.rows(f))
    assert kinds == [("body", 0), ("body", 1), ("desc", 0)]


def test_process_paths_removes_stale_tail_when_file_shrinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
) -> None:
    """Issue #1946: after a file shrinks, the rows it no longer produces
    (old body tail) are gone — every remaining chunk matches current content."""
    f = tmp_path / "long.md"
    f.write_text(_long_note(12), encoding="utf-8")
    backend = store_backend
    daemon._process_paths(
        backend,
        {f.resolve()},
        _FakeProvider(),
        Liveness(_liveness_timeout()),
    )
    assert store_backend.rows(f) == {("desc", 0), ("body", 0), ("body", 1), ("body", 2)}

    f.write_text(_long_note(3), encoding="utf-8")
    daemon._process_paths(
        backend,
        {f.resolve()},
        _FakeProvider(),
        Liveness(_liveness_timeout()),
    )
    assert store_backend.rows(f) == {("desc", 0), ("body", 0)}
    assert backend.all_meta()[str(f.resolve())][1] == content_hash(f.read_text())


def test_process_paths_removes_desc_row_when_description_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
) -> None:
    """A removed description removes its row; the body stays current."""
    f = tmp_path / "note.md"
    f.write_text("---\ndescription: old description\n---\n\nbody text", encoding="utf-8")
    backend = store_backend
    daemon._process_paths(
        backend,
        {f.resolve()},
        _FakeProvider(),
        Liveness(_liveness_timeout()),
    )
    assert store_backend.rows(f) == {("desc", 0), ("body", 0)}

    f.write_text("body text", encoding="utf-8")
    daemon._process_paths(
        backend,
        {f.resolve()},
        _FakeProvider(),
        Liveness(_liveness_timeout()),
    )
    assert store_backend.rows(f) == {("body", 0)}
    assert backend.all_meta()[str(f.resolve())][1] == content_hash(f.read_text())


def test_process_paths_removes_all_rows_when_file_becomes_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
) -> None:
    """An emptied file leaves no rows behind (issue #1946)."""
    f = tmp_path / "note.md"
    f.write_text("---\ndescription: old description\n---\n\nbody text", encoding="utf-8")
    backend = store_backend
    daemon._process_paths(
        backend,
        {f.resolve()},
        _FakeProvider(),
        Liveness(_liveness_timeout()),
    )
    assert store_backend.rows(f) == {("desc", 0), ("body", 0)}

    f.write_text("", encoding="utf-8")
    daemon._process_paths(
        backend,
        {f.resolve()},
        _FakeProvider(),
        Liveness(_liveness_timeout()),
    )
    assert store_backend.rows(f) == set()
    assert backend.all_meta() == {}


def test_process_paths_calls_upsert_many_once_across_embed_batches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RecordingBackend(StoreBackend):
        def __init__(self) -> None:
            super().__init__(tmp_path / "recording" / "vectors.npz", dim=_DIM, fingerprint=_FP)
            self.calls: list[list[tuple[str, float, str, np.ndarray, str, int]]] = []

        def upsert_many(self, rows: Sequence[tuple[str, float, str, np.ndarray, str, int]]) -> None:
            self.calls.append(list(rows))
            super().upsert_many(rows)

    first = tmp_path / "first.md"
    first.write_text("---\ndescription: first description\n---\nfirst body", encoding="utf-8")
    second = tmp_path / "second.md"
    second.write_text("second body", encoding="utf-8")
    monkeypatch.setattr(daemon, "_BATCH_SIZE", 2)
    backend = _RecordingBackend()

    daemon._process_paths(
        backend,
        {first.resolve(), second.resolve()},
        _FakeProvider(),
        Liveness(_liveness_timeout()),
    )

    assert len(backend.calls) == 1
    assert len(backend.calls[0]) == 3
    assert {row[0] for row in backend.calls[0]} == {str(first.resolve()), str(second.resolve())}


def test_partial_embedding_failure_keeps_old_rows_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file whose embedding fails part way keeps its previous rows whole —
    all-old is the recoverable consistent state; the hash stays mismatching so
    the next fs event or follow-up reconcile retries it (issue #1946)."""

    class _FailSecondBatchProvider(_FakeProvider):
        def embed_batch(self, texts: list[str]) -> np.ndarray:
            if self.embed_batch_count == 1:
                raise EmbeddingAPIError("second batch failed")
            return super().embed_batch(texts)

    class _RecordingBackend(StoreBackend):
        def __init__(self) -> None:
            super().__init__(tmp_path / "recording" / "vectors.npz", dim=_DIM, fingerprint=_FP)
            self.calls: list[list[tuple[str, float, str, np.ndarray, str, int]]] = []

        def upsert_many(self, rows: Sequence[tuple[str, float, str, np.ndarray, str, int]]) -> None:
            self.calls.append(list(rows))
            super().upsert_many(rows)

    note = tmp_path / "note.md"
    note.write_text("---\ndescription: description\n---\nbody", encoding="utf-8")
    monkeypatch.setattr(daemon, "_BATCH_SIZE", 1)
    backend = _RecordingBackend()
    daemon._process_paths(
        backend,
        {note.resolve()},
        _FakeProvider(),
        Liveness(_liveness_timeout()),
    )
    old_meta = backend.all_meta()

    note.write_text("---\ndescription: new description\n---\nnew body", encoding="utf-8")
    backend.calls.clear()
    with pytest.raises(EmbeddingAPIError, match="second batch failed"):
        daemon._process_paths(
            backend,
            {note.resolve()},
            _FailSecondBatchProvider(),
            Liveness(_liveness_timeout()),
        )

    # Nothing was written for the partially-embedded file; the old rows stand.
    assert backend.calls == []
    assert backend.all_meta() == old_meta


def test_complete_file_still_commits_when_another_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
) -> None:
    """Files whose rows are ALL embedded commit even when a sibling file
    fails mid-embedding (issue #1946)."""

    class _FailOnSecondFileProvider(_FakeProvider):
        def embed_batch(self, texts: list[str]) -> np.ndarray:
            if self.embed_batch_count == 2:
                raise EmbeddingAPIError("third batch failed")
            return super().embed_batch(texts)

    monkeypatch.setattr(daemon, "_BATCH_SIZE", 1)
    backend = store_backend
    complete = tmp_path / "complete.md"
    complete.write_text("complete body", encoding="utf-8")
    partial = tmp_path / "partial.md"
    partial.write_text("---\ndescription: p\n---\nbody", encoding="utf-8")

    with pytest.raises(EmbeddingAPIError, match="third batch failed"):
        daemon._process_paths(
            backend,
            {complete.resolve(), partial.resolve()},
            _FailOnSecondFileProvider(),
            Liveness(_liveness_timeout()),
        )

    meta = backend.all_meta()
    assert str(complete.resolve()) in meta
    assert str(partial.resolve()) not in meta


def test_reconcile_retry_schedule_backoff_and_reset() -> None:
    clock = SimpleNamespace(now=100.0)
    retry = daemon._ReconcileRetrySchedule(base_s=2.0, cap_s=9.0, clock=lambda: clock.now)
    _assert_retry_idle(retry)
    # Each incomplete signal advances the ladder, including an idle drain failure.
    for delay in [2.0, 4.0, 8.0, 9.0, 9.0]:
        _assert_one_ladder_rung(retry, clock, delay)
    # A quota outage may last indefinitely; the capped backoff must not overflow.
    for _ in range(1100):
        assert retry.record_incomplete() == 9.0
    retry.record_pass_complete()
    _assert_retry_idle(retry)
    assert retry.ensure_scheduled() == 2.0  # an idle drain failure takes the base rung
    assert retry.record_incomplete() == 4.0  # its incomplete follow-up takes the next


def test_reconcile_health_payload() -> None:
    now = 0.0
    retry = daemon._ReconcileRetrySchedule(base_s=2.0, cap_s=4.0, clock=lambda: now)
    idle = {"reconcile_pending": False, "reconcile_retry_in_s": None}
    assert daemon._reconcile_health(retry) == idle
    retry.ensure_scheduled()
    assert daemon._reconcile_health(retry) == {
        "reconcile_pending": True,
        "reconcile_retry_in_s": 2.0,
    }
    now = 0.76
    assert daemon._reconcile_health(retry)["reconcile_retry_in_s"] == 1.2
    retry.record_pass_complete()
    assert daemon._reconcile_health(retry) == idle


async def test_reconcile_retry_drain_loop_converges_after_embed_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    reconcile = Mock(wraps=daemon._reconcile)
    monkeypatch.setattr(daemon, "_reconcile", reconcile)
    # More than one reconcile chunk: recovery must fill the entire original gap.
    files = {tmp_path / f"note-{i:03d}.md" for i in range(70)}
    for note in files:
        note.write_text(f"content of {note.name}")
    monkeypatch.setattr(daemon, "_LOOP_INTERVAL_S", 0.01)
    backend = store_backend
    provider = _FlakyProvider(failures=3)
    liveness = Liveness(1.0)
    retry = daemon._ReconcileRetrySchedule(base_s=0.05, cap_s=0.1)
    assert await asyncio.to_thread(daemon._reconcile, backend, provider, liveness) is False
    retry.record_incomplete()
    with caplog.at_level(logging.INFO, logger="services.derived.memory_indexer.daemon"):
        task = asyncio.create_task(
            daemon._drain_loop(backend, queue.Queue(), liveness, provider, retry)
        )
        try:
            async with asyncio.timeout(5.0):
                while retry.pending:
                    assert not task.done()
                    assert liveness.is_alive()
                    await asyncio.sleep(0.01)
            await _assert_gap_closed(backend, files, retry, liveness, reconcile, caplog)
        finally:
            await _cancel_quietly(task)


async def test_reconcile_retry_drain_failure_arms_schedule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_backend: StoreBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    note = tmp_path / "changed.md"
    note.write_text("new content")
    monkeypatch.setattr(daemon, "_LOOP_INTERVAL_S", 0.01)
    backend = store_backend
    provider = _FlakyProvider(failures=1)
    dirty: queue.Queue[Path] = queue.Queue()
    dirty.put(note)
    liveness = Liveness(1.0)
    retry = daemon._ReconcileRetrySchedule(base_s=0.05, cap_s=0.1)
    task = asyncio.create_task(daemon._drain_loop(backend, dirty, liveness, provider, retry))
    try:
        async with asyncio.timeout(5.0):
            while not retry.pending:
                assert not task.done()
                await asyncio.sleep(0.005)
            assert dirty.empty()
            assert "embed failed for a batch of 1 path(s)" in caplog.text
            assert "follow-up reconcile scheduled" in caplog.text
            while retry.pending:
                assert not task.done()
                assert liveness.is_alive()
                await asyncio.sleep(0.01)
        assert liveness.is_alive()
        assert set(await asyncio.to_thread(backend.all_meta)) == {str(note)}
        assert provider.attempts == 2
        assert not retry.pending
    finally:
        await _cancel_quietly(task)


async def test_reconcile_retry_beats_between_failed_pass_and_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = {"reconcile": 0, "batch": 0}

    def slow_reconcile(*args: Any) -> bool:
        time.sleep(0.4)
        completed["reconcile"] += 1
        return False

    def slow_batch(*args: Any) -> None:
        time.sleep(0.4)
        completed["batch"] += 1
        raise EmbeddingAPIError("429 Too Many Requests")

    monkeypatch.setattr(daemon, "_reconcile", slow_reconcile)
    monkeypatch.setattr(daemon, "_process_paths", slow_batch)
    monkeypatch.setattr(daemon, "_LOOP_INTERVAL_S", 0.01)
    dirty: queue.Queue[Path] = queue.Queue()
    dirty.put(tmp_path / "note.md")
    retry = daemon._ReconcileRetrySchedule(base_s=0.01, cap_s=0.1)
    retry.record_incomplete()
    # Either operation fits below the ceiling, but their combined tails do not.
    liveness = Liveness(0.6)
    task = asyncio.create_task(daemon._drain_loop(Mock(), dirty, liveness, _FakeProvider(), retry))
    started = time.monotonic()
    try:
        while time.monotonic() - started < 1.3:
            await asyncio.sleep(0.01)
            assert not task.done()
            assert liveness.is_alive(), completed
        assert completed["reconcile"] >= 1
        assert completed["batch"] == 1
    finally:
        await _cancel_quietly(task)


@pytest.mark.usefixtures("owned_config_environment")
async def test_run_unknown_provider_fails_before_health_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    start_health_server = AsyncMock()
    monkeypatch.setattr(daemon, "_is_running", lambda: False)
    monkeypatch.setattr(daemon, "_write_pidfile", lambda: None)
    monkeypatch.setattr(daemon, "start_health_server", start_health_server)
    boot = ConfigBoot()
    monkeypatch.setattr(boot.view.services, "embedding_backend", "unknown-provider")

    with pytest.raises(SystemExit) as exc:
        await daemon.run(config=boot)

    assert exc.value.code == 1
    assert "FATAL: unknown embedding provider 'unknown-provider'" in capsys.readouterr().err
    start_health_server.assert_not_awaited()


@pytest.mark.usefixtures("owned_config_environment")
async def test_run_arms_retry_when_startup_reconcile_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from services.derived.memory_indexer.backends.probe import ProbeResult

    backend = Mock()
    backend.name = "fake"
    provider = _FakeProvider()
    reconcile = Mock(return_value=False)
    drain = AsyncMock()
    monkeypatch.setattr(daemon, "_is_running", lambda: False)
    monkeypatch.setattr(daemon, "_write_pidfile", lambda: None)
    monkeypatch.setattr(daemon, "_remove_pidfile", lambda: None)
    monkeypatch.setattr(daemon, "start_health_server", AsyncMock())
    monkeypatch.setattr(daemon, "stop_health_server", AsyncMock())
    monkeypatch.setattr(daemon, "_endpoint", lambda: Mock(health_port=0))

    provider_inputs: list[tuple[str, float, str | None]] = []

    def get_provider(
        name: str,
        *,
        catalog: object,
        timeout_reader: Callable[[], float],
        api_key_reader: Callable[[], str | None],
    ) -> _FakeProvider:
        del catalog
        provider_inputs.append((name, timeout_reader(), api_key_reader()))
        return provider

    monkeypatch.setattr(daemon, "get_provider", get_provider)
    monkeypatch.setattr(daemon, "probe_backend", Mock(return_value=ProbeResult(message=None)))
    monkeypatch.setattr(daemon, "_connect_backend_with_retry", AsyncMock(return_value=backend))
    monkeypatch.setattr(daemon, "Observer", Mock())
    monkeypatch.setattr(daemon, "_reconcile", reconcile)
    monkeypatch.setattr(daemon, "_drain_loop", drain)
    boot = ConfigBoot()
    boot.set_field("memory_indexer_reconcile_retry_backoff_seconds", 7.0)
    boot.set_field("embedding_backend", "gemini")
    boot.set_field("memory_embed_timeout_seconds", 17.0)
    boot.set_field("gemini_api_key", SecretStr("root-key"))

    with caplog.at_level(logging.WARNING, logger="services.derived.memory_indexer.daemon"):
        await daemon.run(config=boot)

    assert provider_inputs == [("gemini", 17.0, "root-key")]
    reconcile.assert_called_once()
    assert reconcile.call_args.args[:2] == (backend, provider)
    drain.assert_awaited_once()
    assert drain.await_args is not None
    retry = drain.await_args.kwargs["retry"]
    assert isinstance(retry, daemon._ReconcileRetrySchedule)
    assert retry.pending
    retry_in = retry.retry_in_s()
    assert retry_in is not None and 0 < retry_in <= 7.0
    assert "startup reconcile incomplete; retry scheduled in 7.0s" in caplog.text


async def test_reconcile_retry_wait_keeps_liveness_and_cancels_promptly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon, "_LOOP_INTERVAL_S", 0.01)
    # Wait far longer than the heartbeat ceiling while keeping the test bounded.
    retry = daemon._ReconcileRetrySchedule(base_s=2.0, cap_s=2.0)
    retry.record_incomplete()
    liveness = Liveness(0.15)
    backend = Mock()
    task = asyncio.create_task(
        daemon._drain_loop(backend, queue.Queue(), liveness, _FakeProvider(), retry)
    )
    started = time.monotonic()
    try:
        while time.monotonic() - started < 0.4:
            await asyncio.sleep(0.01)
            assert liveness.is_alive()
            assert retry.pending
            assert not task.done()
        backend.all_meta.assert_not_called()  # still waiting, no pass ran yet
    finally:
        cancelled_at = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)
        assert time.monotonic() - cancelled_at < 0.5

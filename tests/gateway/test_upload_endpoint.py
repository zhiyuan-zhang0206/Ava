"""File upload endpoint tests — POST /api/agents/{agent_id}/uploads.

Verifies: one or more files saved to the right path, a single inbound
message inserted per request (the whole batch is one notification),
agent-not-found -> 404, filename sanitization.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app


@pytest.mark.asyncio
async def test_upload_lock_cache_evicts_idle_locks_but_keeps_held_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy agent's upload mutex remains stable while stale idle locks are evicted."""
    from gateway.routers import uploads as uploads_router

    uploads_router._agent_locks.clear()
    monkeypatch.setattr(uploads_router, "_AGENT_LOCK_CACHE_MAX_ENTRIES", 2)
    held: asyncio.Lock | None = None
    try:
        lock = await uploads_router._agent_lock(1)
        held = lock
        await lock.acquire()
        await uploads_router._agent_lock(2)
        await uploads_router._agent_lock(3)

        assert list(uploads_router._agent_locks) == [1, 3]
    finally:
        if held is not None and held.locked():
            held.release()
        uploads_router._agent_locks.clear()


def _inbound_rows(db: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str]]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT content, kind, source FROM inbound_messages WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        return cur.fetchall()


def _spawn_agent() -> int:
    # Enter the app lifespan (the `with` block) so `app.state.db_pool` is
    # initialized for this request. A bare `TestClient(app)` skips startup,
    # leaving the spawn dependent on some earlier test having populated the
    # shared module-level app — which breaks under xdist worker sharding /
    # randomized order (same isolation class as #585).
    with TestClient(app) as client:
        resp = client.post("/api/agents", json={})
        assert resp.status_code == 201
        return resp.json()["id"]


class TestUploadFile:
    def test_upload_single_file_saves_and_inserts_one_inbound(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Happy path: one file -> saved to mocked Downloads dir, one inbound inserted."""
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        with TestClient(app) as client:
            resp = client.post(
                f"/api/agents/{agent_id}/uploads",
                files=[("files", ("hello.txt", b"Hello, world!", "text/plain"))],
            )

        assert resp.status_code == 200
        files = resp.json()["files"]
        assert len(files) == 1
        assert files[0]["filename"] == "hello.txt"
        assert files[0]["size"] == 13
        assert files[0]["content_type"] == "text/plain"

        dest = tmp_path / "Downloads" / f"AvaAgent-{agent_id}" / "hello.txt"
        assert dest.exists()
        assert dest.read_bytes() == b"Hello, world!"
        assert dest.parent.stat().st_mode & 0o777 == 0o700
        assert dest.stat().st_mode & 0o777 == 0o600

        rows = _inbound_rows(db_conn, agent_id)
        assert len(rows) == 1
        content, kind, source = rows[0]
        # The agent runs on this gateway (single-box), so the message carries
        # its LOCAL absolute path — the address it can act on directly.
        assert str(dest) in content
        assert "13 bytes" in content
        assert kind == "chat"
        assert source == "user"

    def test_upload_multiple_files_one_batch_inbound(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A batch of files is each saved, but produces exactly ONE inbound
        that lists every saved path — not one notification per file."""
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        with TestClient(app) as client:
            resp = client.post(
                f"/api/agents/{agent_id}/uploads",
                files=[
                    ("files", ("a.txt", b"aaa", "text/plain")),
                    ("files", ("b.txt", b"bb", "text/plain")),
                    ("files", ("c.txt", b"c", "text/plain")),
                ],
            )

        assert resp.status_code == 200
        files = resp.json()["files"]
        assert [f["filename"] for f in files] == ["a.txt", "b.txt", "c.txt"]
        assert [f["size"] for f in files] == [3, 2, 1]

        base = tmp_path / "Downloads" / f"AvaAgent-{agent_id}"
        for name, body in (("a.txt", b"aaa"), ("b.txt", b"bb"), ("c.txt", b"c")):
            assert (base / name).read_bytes() == body

        rows = _inbound_rows(db_conn, agent_id)
        assert len(rows) == 1
        content = rows[0][0]
        assert "3 files uploaded" in content
        # The agent runs on this gateway, so each line carries the local
        # absolute path the agent can read directly.
        for name in ("a.txt", "b.txt", "c.txt"):
            assert str(base / name) in content

    def test_upload_sanitizes_filename_slashes(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Filename with / is sanitized (replaced with _)."""
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        with TestClient(app) as client:
            resp = client.post(
                f"/api/agents/{agent_id}/uploads",
                files=[("files", ("etc/passwd.txt", b"data", "text/plain"))],
            )

        assert resp.status_code == 200
        assert resp.json()["files"][0]["filename"] == "etc_passwd.txt"

        dest = tmp_path / "Downloads" / f"AvaAgent-{agent_id}" / "etc_passwd.txt"
        assert dest.exists()

    def test_upload_agent_not_found_returns_404(self) -> None:
        """Upload to nonexistent agent -> 404."""
        with TestClient(app) as client:
            resp = client.post(
                "/api/agents/99999/uploads",
                files=[("files", ("x.txt", b"x", "text/plain"))],
            )
        assert resp.status_code == 404

    def test_upload_preserves_path_in_response(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Response includes the full saved path."""
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        with TestClient(app) as client:
            resp = client.post(
                f"/api/agents/{agent_id}/uploads",
                files=[("files", ("photo.png", b"\x89PNG", "image/png"))],
            )

        f = resp.json()["files"][0]
        assert f["path"].endswith(f"AvaAgent-{agent_id}/photo.png")
        assert f["content_type"] == "image/png"

    def test_upload_empty_file(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty file (0 bytes) is accepted."""
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        with TestClient(app) as client:
            resp = client.post(
                f"/api/agents/{agent_id}/uploads",
                files=[("files", ("empty.txt", b"", "text/plain"))],
            )

        assert resp.status_code == 200
        assert resp.json()["files"][0]["size"] == 0
        rows = _inbound_rows(db_conn, agent_id)
        assert "0 bytes" in rows[0][0]


class TestUploadLimits:
    """Audit round-2 security P1-2: the upload endpoint is bounded — per-file
    size, per-agent total quota, per-agent file count — before any byte lands."""

    def test_per_file_limit_rejected_413(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        import gateway.routers.uploads as uploads_router

        monkeypatch.setattr(uploads_router, "MAX_UPLOAD_BYTES", 10)
        with TestClient(app) as client:
            resp = client.post(
                f"/api/agents/{agent_id}/uploads",
                files=[("files", ("big.bin", b"x" * 11, "application/octet-stream"))],
            )
        assert resp.status_code == 413
        assert not (tmp_path / "Downloads" / f"AvaAgent-{agent_id}" / "big.bin").exists()
        assert _inbound_rows(db_conn, agent_id) == []  # no notification on refusal

    def test_agent_quota_rejected_413(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two files crossing the total quota: the first lands, the second 413s."""
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        import gateway.routers.uploads as uploads_router

        monkeypatch.setattr(uploads_router, "MAX_AGENT_UPLOAD_BYTES", 30)
        with TestClient(app) as client:
            ok = client.post(
                f"/api/agents/{agent_id}/uploads",
                files=[("files", ("a.txt", b"a" * 20, "text/plain"))],
            )
            over = client.post(
                f"/api/agents/{agent_id}/uploads",
                files=[("files", ("b.txt", b"b" * 20, "text/plain"))],
            )
        assert ok.status_code == 200
        assert over.status_code == 413
        base = tmp_path / "Downloads" / f"AvaAgent-{agent_id}"
        assert (base / "a.txt").exists()
        assert not (base / "b.txt").exists()

    def test_agent_quota_rejects_entire_oversized_batch(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A batch over the byte quota does not leave its earlier files behind."""
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        import gateway.routers.uploads as uploads_router

        monkeypatch.setattr(uploads_router, "MAX_AGENT_UPLOAD_BYTES", 5)
        with TestClient(app) as client:
            resp = client.post(
                f"/api/agents/{agent_id}/uploads",
                files=[
                    ("files", ("a.txt", b"aaa", "text/plain")),
                    ("files", ("b.txt", b"bbb", "text/plain")),
                ],
            )

        base = tmp_path / "Downloads" / f"AvaAgent-{agent_id}"
        assert resp.status_code == 413
        assert not (base / "a.txt").exists()
        assert not (base / "b.txt").exists()
        assert _inbound_rows(db_conn, agent_id) == []
        temp_dir = base / ".tmp"
        assert not temp_dir.exists() or list(temp_dir.iterdir()) == []

    def test_file_count_cap_rejected_413(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        import gateway.routers.uploads as uploads_router

        monkeypatch.setattr(uploads_router, "MAX_AGENT_UPLOAD_FILES", 1)
        with TestClient(app) as client:
            ok = client.post(
                f"/api/agents/{agent_id}/uploads",
                files=[("files", ("a.txt", b"a", "text/plain"))],
            )
            over = client.post(
                f"/api/agents/{agent_id}/uploads",
                files=[("files", ("b.txt", b"b", "text/plain"))],
            )
        assert ok.status_code == 200
        assert over.status_code == 413

    def test_file_count_cap_rejects_entire_batch(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A batch over the file-count cap does not partially commit."""
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        import gateway.routers.uploads as uploads_router

        monkeypatch.setattr(uploads_router, "MAX_AGENT_UPLOAD_FILES", 1)
        with TestClient(app) as client:
            resp = client.post(
                f"/api/agents/{agent_id}/uploads",
                files=[
                    ("files", ("a.txt", b"a", "text/plain")),
                    ("files", ("b.txt", b"b", "text/plain")),
                ],
            )

        base = tmp_path / "Downloads" / f"AvaAgent-{agent_id}"
        assert resp.status_code == 413
        assert not (base / "a.txt").exists()
        assert not (base / "b.txt").exists()

    def test_concurrent_batches_share_agent_quota(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Concurrent uploads serially consume one agent's shared byte quota."""
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        import gateway.routers.uploads as uploads_router

        monkeypatch.setattr(uploads_router, "MAX_AGENT_UPLOAD_BYTES", 10)
        base = tmp_path / "Downloads" / f"AvaAgent-{agent_id}"
        final_write_barrier = threading.Barrier(2)
        original_write_bytes = Path.write_bytes

        def _block_direct_final_write(path: Path, contents: bytes) -> int:
            if path.parent == base:
                final_write_barrier.wait(timeout=2)
            return original_write_bytes(path, contents)

        monkeypatch.setattr(Path, "write_bytes", _block_direct_final_write)
        with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as executor:
            responses = [
                executor.submit(
                    client.post,
                    f"/api/agents/{agent_id}/uploads?deliver=false",
                    files=[("files", (name, b"x" * 6, "text/plain"))],
                )
                for name in ("a.txt", "b.txt")
            ]
            statuses = [response.result().status_code for response in responses]

        assert sorted(statuses) == [200, 413]
        stored = [path for path in base.iterdir() if path.is_file()]
        assert sum(path.stat().st_size for path in stored) <= 10
        assert len(stored) == 1
        assert stored[0].read_bytes() == b"x" * 6

    def test_successful_upload_sweeps_stale_temp_files(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Crash leftovers in .tmp do not count toward quota and are removed."""
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        import gateway.routers.uploads as uploads_router

        monkeypatch.setattr(uploads_router, "MAX_AGENT_UPLOAD_BYTES", 5)
        base = tmp_path / "Downloads" / f"AvaAgent-{agent_id}"
        temp_dir = base / ".tmp"
        temp_dir.mkdir(parents=True)
        (temp_dir / "stale").write_bytes(b"x" * 100)

        with TestClient(app) as client:
            resp = client.post(
                f"/api/agents/{agent_id}/uploads?deliver=false",
                files=[("files", ("fresh.txt", b"fresh", "text/plain"))],
            )

        assert resp.status_code == 200
        assert (base / "fresh.txt").read_bytes() == b"fresh"
        assert list(temp_dir.iterdir()) == []

    def test_failed_promotion_removes_new_final_names(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rename failure restores the directory to its pre-batch state."""
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        import gateway.routers.uploads as uploads_router

        base = tmp_path / "Downloads" / f"AvaAgent-{agent_id}"
        temp_dir = base / ".tmp"
        original_replace = uploads_router.os.replace
        promotions = 0

        def _fail_second_promotion(src: str | Path, dest: str | Path) -> None:
            nonlocal promotions
            if Path(src).parent == temp_dir and Path(dest).parent == base:
                promotions += 1
                if promotions == 2:
                    raise OSError("injected rename failure")
            original_replace(src, dest)

        monkeypatch.setattr(uploads_router.os, "replace", _fail_second_promotion)
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(
                f"/api/agents/{agent_id}/uploads?deliver=false",
                files=[
                    ("files", ("a.txt", b"a", "text/plain")),
                    ("files", ("b.txt", b"b", "text/plain")),
                ],
            )

        assert resp.status_code == 500
        assert not (base / "a.txt").exists()
        assert not (base / "b.txt").exists()
        assert list(temp_dir.iterdir()) == []


class TestUploadUrlAndAttachMode:
    def test_response_includes_reference_url(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with TestClient(app) as client:
            resp = client.post(
                f"/api/agents/{agent_id}/uploads",
                files=[("files", ("shot.png", b"\x89PNG", "image/png"))],
            )
        assert resp.status_code == 200
        assert resp.json()["files"][0]["url"] == f"/api/agents/{agent_id}/uploads/shot.png"

    def test_deliver_false_skips_inbound(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The native-attachment path saves silently — no inbound queued."""
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with TestClient(app) as client:
            resp = client.post(
                f"/api/agents/{agent_id}/uploads?deliver=false",
                files=[("files", ("shot.png", b"\x89PNG", "image/png"))],
            )
        assert resp.status_code == 200
        assert (tmp_path / "Downloads" / f"AvaAgent-{agent_id}" / "shot.png").exists()
        assert _inbound_rows(db_conn, agent_id) == []  # no notification

    def test_deliver_true_still_notifies(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with TestClient(app) as client:
            client.post(
                f"/api/agents/{agent_id}/uploads?deliver=true",
                files=[("files", ("a.txt", b"x", "text/plain"))],
            )
        assert len(_inbound_rows(db_conn, agent_id)) == 1


class TestServeUpload:
    def test_serves_saved_file(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        raw = b"\x89PNG\r\n\x1a\npixels"
        with TestClient(app) as client:
            client.post(
                f"/api/agents/{agent_id}/uploads?deliver=false",
                files=[("files", ("shot.png", raw, "image/png"))],
            )
            resp = client.get(f"/api/agents/{agent_id}/uploads/shot.png")
        assert resp.status_code == 200
        assert resp.content == raw

    def test_renderable_upload_served_as_attachment(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HTML/SVG uploads are served with attachment + nosniff so a stored
        payload cannot execute in a browser (stored-XSS surface)."""
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with TestClient(app) as client:
            client.post(
                f"/api/agents/{agent_id}/uploads?deliver=false",
                files=[("files", ("page.html", b"<script>alert(1)</script>", "text/html"))],
            )
            resp = client.get(f"/api/agents/{agent_id}/uploads/page.html")
        assert resp.status_code == 200
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["content-disposition"].startswith("attachment")

    def test_plain_upload_served_with_nosniff_only(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with TestClient(app) as client:
            client.post(
                f"/api/agents/{agent_id}/uploads?deliver=false",
                files=[("files", ("shot.png", b"\x89PNG", "image/png"))],
            )
            resp = client.get(f"/api/agents/{agent_id}/uploads/shot.png")
        assert resp.status_code == 200
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert "content-disposition" not in resp.headers

    def test_missing_file_404(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with TestClient(app) as client:
            resp = client.get(f"/api/agents/{agent_id}/uploads/nope.png")
        assert resp.status_code == 404

    def test_traversal_rejected(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with TestClient(app) as client:
            # %2e%2e%2f = ../ — the router path param decodes it; the resolver
            # must refuse to escape the agent upload dir.
            resp = client.get(f"/api/agents/{agent_id}/uploads/..%2f..%2fsecret")
        assert resp.status_code in (400, 404)

    def test_serve_unknown_agent_404(self) -> None:
        with TestClient(app) as client:
            resp = client.get("/api/agents/99999/uploads/x.png")
        assert resp.status_code == 404


class TestRemoteAgentPull:
    """Cross-machine upload: when the agent runs on a remote runner, the
    gateway dispatches an `upload_receive` op so the file lands on the
    runner's local disk, and the inbound message carries THAT path."""

    def test_remote_agent_message_carries_runner_path(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Agent on a remote machine: dispatch returns the runner's local path,
        and the message uses it (not the gateway's)."""
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Point the agent at a remote machine.
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET machine = %s WHERE id = %s", ("mbp-remote", agent_id)
            )
        db_conn.commit()

        runner_path = f"/Users/runner/Downloads/AvaAgent-{agent_id}/hello.txt"

        async def fake_dispatch(target_machine: str, kind: str, payload: dict, **kw):
            assert target_machine == "mbp-remote"
            assert kind == "upload_receive"
            assert payload == {"agent_id": agent_id, "name": "hello.txt"}
            return {"path": runner_path}

        from ops import cluster_rpc

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]

        with TestClient(app) as client:
            resp = client.post(
                f"/api/agents/{agent_id}/uploads",
                files=[("files", ("hello.txt", b"Hello, world!", "text/plain"))],
            )
        assert resp.status_code == 200

        content = _inbound_rows(db_conn, agent_id)[0][0]
        # The agent's LOCAL path on the runner, not the gateway's path.
        assert runner_path in content

    def test_remote_agent_pull_failure_identifies_gateway_location(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Runner unreachable: the upload succeeds and its notification
        identifies the gateway-side path and download route (best-effort pull)."""
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET machine = %s WHERE id = %s", ("mbp-remote", agent_id)
            )
        db_conn.commit()

        async def fake_dispatch(target_machine: str, kind: str, payload: dict, **kw):
            raise RuntimeError("runner unreachable")

        from ops import cluster_rpc

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]

        with TestClient(app) as client:
            resp = client.post(
                f"/api/agents/{agent_id}/uploads",
                files=[("files", ("hello.txt", b"Hello, world!", "text/plain"))],
            )
        assert resp.status_code == 200  # upload never fails on a failed pull
        content = _inbound_rows(db_conn, agent_id)[0][0]
        dest = tmp_path / "Downloads" / f"AvaAgent-{agent_id}" / "hello.txt"
        from shared.machine import machine_name

        assert str(dest) in content
        assert f"machine: {machine_name()}" in content
        assert f"gateway download: /api/agents/{agent_id}/uploads/hello.txt" in content
        assert "local copy unconfirmed on mbp-remote" in content
        with TestClient(app) as client:
            download = client.get(resp.json()["files"][0]["url"])
        assert download.status_code == 200
        assert download.content == b"Hello, world!"

    def test_local_agent_skips_dispatch(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Agent on this gateway: no dispatch; message carries the gateway's
        local path (== the agent's local path)."""
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        called = []

        async def fake_dispatch(target_machine: str, kind: str, payload: dict, **kw):
            called.append((target_machine, kind))  # pyright: ignore[reportUnknownMemberType]
            raise AssertionError("should not dispatch for a local agent")

        from ops import cluster_rpc

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", fake_dispatch)  # pyright: ignore[reportUnknownArgumentType]

        with TestClient(app) as client:
            resp = client.post(
                f"/api/agents/{agent_id}/uploads",
                files=[("files", ("hello.txt", b"Hello, world!", "text/plain"))],
            )
        assert resp.status_code == 200
        assert called == []
        content = _inbound_rows(db_conn, agent_id)[0][0]
        dest = tmp_path / "Downloads" / f"AvaAgent-{agent_id}" / "hello.txt"
        assert str(dest) in content

    @pytest.mark.parametrize(
        "failed", [{"a.txt"}, {"b.txt"}, {"c.txt"}, {"a.txt", "b.txt", "c.txt"}]
    )
    def test_remote_agent_partial_pull_failure_preserves_file_association(
        self,
        db_conn: psycopg.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        failed: set[str],
    ) -> None:
        """Batch upload where only some files pull: the message carries the
        pulled runner paths for successes and the gateway paths for failures —
        each path stays paired with the correct filename and byte count."""
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET machine = %s WHERE id = %s", ("mbp-remote", agent_id)
            )
        db_conn.commit()

        async def fake_dispatch(
            target_machine: str, kind: str, payload: dict[str, object], **kw: object
        ) -> dict[str, str]:
            name = payload["name"]
            assert isinstance(name, str)
            if name in failed:
                raise RuntimeError(f"runner unreachable for {name}")
            return {"path": f"/Users/runner/Downloads/AvaAgent-{agent_id}/{name}"}

        from ops import cluster_rpc

        monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", fake_dispatch)

        with TestClient(app) as client:
            resp = client.post(
                f"/api/agents/{agent_id}/uploads",
                files=[
                    ("files", ("a.txt", b"aaa", "text/plain")),
                    ("files", ("b.txt", b"bb", "text/plain")),
                    ("files", ("c.txt", b"c", "text/plain")),
                ],
            )
        assert resp.status_code == 200
        content = _inbound_rows(db_conn, agent_id)[0][0]
        base = tmp_path / "Downloads" / f"AvaAgent-{agent_id}"
        from shared.machine import machine_name

        lines = content.splitlines()[1:]
        assert len(lines) == 3
        for line, name, size in zip(lines, ("a.txt", "b.txt", "c.txt"), (3, 2, 1), strict=True):
            assert name in line
            assert f"{size} bytes" in line
            if name in failed:
                assert str(base / name) in line
                assert f"machine: {machine_name()}" in line
                assert f"gateway download: /api/agents/{agent_id}/uploads/{name}" in line
                assert "local copy unconfirmed on mbp-remote" in line
            else:
                assert f"/Users/runner/Downloads/AvaAgent-{agent_id}/{name}" in line
                assert "machine: mbp-remote" in line

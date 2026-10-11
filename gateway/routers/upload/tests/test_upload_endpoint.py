"""Silent upload endpoint tests — POST /api/agents/{agent_id}/uploads.

Verifies: one or more files saved as immutable objects under the agent's upload
directory, no inbound queued, key + principal-v1 scope required, `deliver=true`
refused, agent-not-found -> 404, per-file / per-agent quota limits, and the GET
route serving a saved file back.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx2 as httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from tests.fixtures.gateway_config import spawn_via_http


def _upload(
    client: TestClient,
    agent_id: int,
    files: list[tuple[str, tuple[str, bytes, str]]],
    *,
    query: str = "",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST one silent batch under a fresh key and the principal-v1 scope."""
    return client.post(
        f"/api/agents/{agent_id}/uploads{query}",
        headers=headers
        if headers is not None
        else {"Idempotency-Key": uuid.uuid4().hex, "Idempotency-Scope": "principal-v1"},
        files=files,
    )


def _inbound_count(db: psycopg.Connection, agent_id: int) -> int:
    row = db.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id = %s", (agent_id,)
    ).fetchone()
    assert row is not None
    return row[0]


def _spawn_agent() -> int:
    # Enter the app lifespan (the `with` block) so `app.state.db_pool` is
    # initialized for this request. A bare `TestClient(app)` skips startup,
    # leaving the spawn dependent on some earlier test having populated the
    # shared module-level app — which breaks under xdist worker sharding /
    # randomized order (same isolation class as #585).
    with TestClient(app) as client:
        resp = spawn_via_http(client, {})
        assert resp.status_code == 201
        return resp.json()["id"]


def _agent_dir(home: Path, agent_id: int) -> Path:
    return home / "Downloads" / f"AvaAgent-{agent_id}"


def _stored_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.iterdir() if path.is_file())


class TestUploadFile:
    def test_upload_single_file_saves_privately_and_queues_no_inbound(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        with TestClient(app) as client:
            resp = _upload(
                client,
                agent_id,
                [("files", ("hello.txt", b"Hello, world!", "text/plain"))],
            )

        assert resp.status_code == 200, resp.text
        files = resp.json()["files"]
        assert len(files) == 1
        assert files[0]["filename"] == "hello.txt"
        assert files[0]["size"] == 13
        assert files[0]["content_type"] == "text/plain"
        dest = Path(files[0]["path"])
        assert dest.parent == _agent_dir(tmp_path, agent_id)
        assert dest.read_bytes() == b"Hello, world!"
        assert dest.parent.stat().st_mode & 0o777 == 0o700
        assert dest.stat().st_mode & 0o777 == 0o600
        assert _inbound_count(db_conn, agent_id) == 0

    def test_upload_multiple_files_one_batch(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        with TestClient(app) as client:
            resp = _upload(
                client,
                agent_id,
                [
                    ("files", ("a.txt", b"aaa", "text/plain")),
                    ("files", ("b.txt", b"bb", "text/plain")),
                    ("files", ("c.txt", b"c", "text/plain")),
                ],
            )

        assert resp.status_code == 200, resp.text
        files = resp.json()["files"]
        assert [f["filename"] for f in files] == ["a.txt", "b.txt", "c.txt"]
        assert [f["size"] for f in files] == [3, 2, 1]
        assert [Path(f["path"]).read_bytes() for f in files] == [b"aaa", b"bb", b"c"]

    def test_slashes_in_a_display_name_never_reach_the_stored_path(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        with TestClient(app) as client:
            resp = _upload(client, agent_id, [("files", ("etc/passwd.txt", b"data", "text/plain"))])

        assert resp.status_code == 200, resp.text
        file = resp.json()["files"][0]
        assert file["filename"] == "etc/passwd.txt"
        assert Path(file["path"]).parent == _agent_dir(tmp_path, agent_id)
        assert Path(file["path"]).suffix == ".txt"

    def test_upload_agent_not_found_returns_404(self) -> None:
        with TestClient(app) as client:
            resp = _upload(client, 99999, [("files", ("x.txt", b"x", "text/plain"))])
        assert resp.status_code == 404

    def test_upload_empty_file(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        with TestClient(app) as client:
            resp = _upload(client, agent_id, [("files", ("empty.txt", b"", "text/plain"))])

        assert resp.status_code == 200, resp.text
        assert resp.json()["files"][0]["size"] == 0


class TestUploadContract:
    """The route is silent and keyed: nothing is saved without a verified operation identity."""

    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"Idempotency-Key": "k"},
            {"Idempotency-Scope": "principal-v1"},
            {"Idempotency-Key": "k", "Idempotency-Scope": "unsupported"},
        ],
    )
    def test_missing_or_unsupported_identity_is_refused_before_any_effect(
        self,
        db_conn: psycopg.Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        headers: dict[str, str],
    ) -> None:
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        with TestClient(app) as client:
            resp = _upload(
                client,
                agent_id,
                [("files", ("a.txt", b"x", "text/plain"))],
                headers=headers,
            )

        assert resp.status_code == 422
        assert not _agent_dir(tmp_path, agent_id).exists()
        row = db_conn.execute("SELECT count(*) FROM agent_upload_batches").fetchone()
        assert row is not None and row[0] == 0

    def test_deliver_true_is_refused_and_saves_nothing(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        with TestClient(app) as client:
            resp = _upload(
                client,
                agent_id,
                [("files", ("a.txt", b"x", "text/plain"))],
                query="?deliver=true",
            )

        assert resp.status_code == 422
        assert "/api/keyed/v1/agents/{id}/uploads" in resp.json()["detail"]
        assert not _agent_dir(tmp_path, agent_id).exists()
        assert _inbound_count(db_conn, agent_id) == 0

    def test_response_url_serves_the_saved_object(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        with TestClient(app) as client:
            resp = _upload(client, agent_id, [("files", ("shot.png", b"\x89PNG", "image/png"))])
            url = resp.json()["files"][0]["url"]
            served = client.get(url)

        assert url == f"/api/agents/{agent_id}/uploads/{Path(resp.json()['files'][0]['path']).name}"
        assert served.status_code == 200
        assert served.content == b"\x89PNG"
        assert _inbound_count(db_conn, agent_id) == 0


class TestUploadLimits:
    """The upload endpoint is bounded — per-file size, per-agent total quota,
    per-agent file count — before any byte lands."""

    def test_per_file_limit_rejected_413(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        import gateway.routers.upload.router as uploads_router

        monkeypatch.setattr(uploads_router, "MAX_UPLOAD_BYTES", 10)
        with TestClient(app) as client:
            resp = _upload(
                client,
                agent_id,
                [("files", ("big.bin", b"x" * 11, "application/octet-stream"))],
            )
        assert resp.status_code == 413
        assert not _agent_dir(tmp_path, agent_id).exists()

    def test_agent_quota_rejected_413(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two batches crossing the total quota: the first lands, the second 413s."""
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        import gateway.routers.upload.router as uploads_router

        monkeypatch.setattr(uploads_router, "MAX_AGENT_UPLOAD_BYTES", 30)
        with TestClient(app) as client:
            ok = _upload(client, agent_id, [("files", ("a.txt", b"a" * 20, "text/plain"))])
            over = _upload(client, agent_id, [("files", ("b.txt", b"b" * 20, "text/plain"))])
        assert ok.status_code == 200
        assert over.status_code == 413
        assert len(_stored_files(_agent_dir(tmp_path, agent_id))) == 1

    def test_agent_quota_rejects_entire_oversized_batch(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A batch over the byte quota does not leave its earlier files behind."""
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        import gateway.routers.upload.router as uploads_router

        monkeypatch.setattr(uploads_router, "MAX_AGENT_UPLOAD_BYTES", 5)
        with TestClient(app) as client:
            resp = _upload(
                client,
                agent_id,
                [
                    ("files", ("a.txt", b"aaa", "text/plain")),
                    ("files", ("b.txt", b"bbb", "text/plain")),
                ],
            )

        assert resp.status_code == 413
        directory = _agent_dir(tmp_path, agent_id)
        assert not directory.exists() or _stored_files(directory) == []

    def test_file_count_cap_rejected_413(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        import gateway.routers.upload.router as uploads_router

        monkeypatch.setattr(uploads_router, "MAX_AGENT_UPLOAD_FILES", 1)
        with TestClient(app) as client:
            ok = _upload(client, agent_id, [("files", ("a.txt", b"a", "text/plain"))])
            over = _upload(client, agent_id, [("files", ("b.txt", b"b", "text/plain"))])
        assert ok.status_code == 200
        assert over.status_code == 413

    def test_file_count_cap_rejects_entire_batch(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A batch over the file-count cap does not partially commit."""
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        import gateway.routers.upload.router as uploads_router

        monkeypatch.setattr(uploads_router, "MAX_AGENT_UPLOAD_FILES", 1)
        with TestClient(app) as client:
            resp = _upload(
                client,
                agent_id,
                [
                    ("files", ("a.txt", b"a", "text/plain")),
                    ("files", ("b.txt", b"b", "text/plain")),
                ],
            )

        assert resp.status_code == 413
        directory = _agent_dir(tmp_path, agent_id)
        assert not directory.exists() or _stored_files(directory) == []

    def test_concurrent_batches_share_agent_quota(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Concurrent uploads serially consume one agent's shared byte quota."""
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        import gateway.routers.upload.router as uploads_router

        monkeypatch.setattr(uploads_router, "MAX_AGENT_UPLOAD_BYTES", 10)
        with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as executor:
            responses = [
                executor.submit(
                    _upload, client, agent_id, [("files", (name, b"x" * 6, "text/plain"))]
                )
                for name in ("a.txt", "b.txt")
            ]
            statuses = [response.result().status_code for response in responses]

        assert sorted(statuses) == [200, 413]
        stored = _stored_files(_agent_dir(tmp_path, agent_id))
        assert len(stored) == 1
        assert stored[0].read_bytes() == b"x" * 6


class TestServeUpload:
    def test_serves_saved_file(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        raw = b"\x89PNG\r\n\x1a\npixels"
        with TestClient(app) as client:
            saved = _upload(client, agent_id, [("files", ("shot.png", raw, "image/png"))])
            resp = client.get(saved.json()["files"][0]["url"])
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
            saved = _upload(
                client,
                agent_id,
                [("files", ("page.html", b"<script>alert(1)</script>", "text/html"))],
            )
            resp = client.get(saved.json()["files"][0]["url"])
        assert resp.status_code == 200
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["content-disposition"].startswith("attachment")

    def test_plain_upload_served_with_nosniff_only(
        self, db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent_id = _spawn_agent()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with TestClient(app) as client:
            saved = _upload(client, agent_id, [("files", ("shot.png", b"\x89PNG", "image/png"))])
            resp = client.get(saved.json()["files"][0]["url"])
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

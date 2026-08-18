"""Unit tests for ops/ops_uploads.py — the runner-side half of cross-machine
file uploads (the `upload_receive` op).

The gateway stores every upload on its own disk; this op pulls one file from
the gateway over HTTP into the runner's local ~/Downloads/AvaAgent-<id>/ so
an agent running on this remote host can read it off its own disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ops.ops_uploads import upload_receive_op
from ops.rpc_schemas import UploadReceivePayload


def _stub_gateway(monkeypatch: pytest.MonkeyPatch, raw: bytes) -> dict[str, list[str]]:
    """Stand in for the gateway's GET /uploads endpoint: the op module's
    http_get returns `raw`. Returns the seen URLs."""
    import ops.ops_uploads as mod

    seen: dict[str, list[str]] = {"urls": []}

    def _fake_get(url: str, **kwargs: object) -> object:
        seen["urls"].append(url)

        class _Resp:
            content = raw
            status_code = 200

            def raise_for_status(self) -> None:
                return None

        return _Resp()

    monkeypatch.setattr(mod, "http_get", _fake_get)
    monkeypatch.setattr(mod, "gateway_api_base", lambda: "http://gw.test:8000")
    return seen


class TestUploadReceiveOp:
    def test_pulls_upload_into_local_uploads_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Happy path: fetches the file from the gateway and writes it into
        ~/Downloads/AvaAgent-<id>/ on this runner; returns the local path."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        raw = b"Hello, world!"
        seen = _stub_gateway(monkeypatch, raw)

        result = upload_receive_op(UploadReceivePayload(agent_id=7, name="report.pdf"))

        assert seen["urls"] == ["http://gw.test:8000/api/agents/7/uploads/report.pdf"]
        dest = tmp_path / "Downloads" / "AvaAgent-7" / "report.pdf"
        assert dest.read_bytes() == raw
        assert result.path == str(dest)

    def test_sanitizes_filename(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A name with separators is sanitized before it hits the disk."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _stub_gateway(monkeypatch, b"data")

        result = upload_receive_op(UploadReceivePayload(agent_id=7, name="a/b.txt"))

        dest = tmp_path / "Downloads" / "AvaAgent-7" / "a_b.txt"
        assert dest.read_bytes() == b"data"
        assert result.path == str(dest)

    def test_gateway_unreachable_raises_oserror(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A failed fetch raises OSError — the daemon surfaces it as a 'failed'
        op result and the gateway degrades to the URL-only message."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        import ops.ops_uploads as mod

        def _fake_get(url: str, **kwargs: object) -> object:
            import httpx

            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(mod, "http_get", _fake_get)
        monkeypatch.setattr(mod, "gateway_api_base", lambda: "http://gw.test:8000")

        with pytest.raises(OSError):
            upload_receive_op(UploadReceivePayload(agent_id=7, name="report.pdf"))

    def test_gateway_404_raises_oserror(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A 404 (file gone on the gateway) raises OSError, not a silent
        partial write."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        import ops.ops_uploads as mod

        class _Resp404:
            content = b""

            def raise_for_status(self) -> None:
                import httpx

                raise httpx.HTTPStatusError(
                    "404 not found",
                    request=httpx.Request("GET", "http://gw.test"),
                    response=None,  # type: ignore[arg-type]
                )

        def _fake_404(url: str, **kwargs: object) -> object:
            return _Resp404()

        monkeypatch.setattr(mod, "http_get", _fake_404)
        monkeypatch.setattr(mod, "gateway_api_base", lambda: "http://gw.test:8000")

        with pytest.raises(OSError):
            upload_receive_op(UploadReceivePayload(agent_id=7, name="report.pdf"))
        # Nothing was written.
        assert not (tmp_path / "Downloads" / "AvaAgent-7" / "report.pdf").exists()

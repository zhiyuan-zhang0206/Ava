"""shared.uploads — url <-> path mapping + traversal-safe resolution + base64.

Pure unit (no DB / no gateway). The traversal guard is security-critical: a
crafted image reference must never resolve outside the agent's upload dir.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from shared import uploads


class TestParseUploadUrl:
    def test_valid(self) -> None:
        assert uploads.parse_upload_url("/api/agents/7/uploads/foo.png") == (7, "foo.png")

    def test_rejects_foreign_url(self) -> None:
        assert uploads.parse_upload_url("http://evil.example/x.png") is None
        assert uploads.parse_upload_url("/api/agents/7/timeline") is None
        assert uploads.parse_upload_url("/api/agents/abc/uploads/x.png") is None

    def test_keeps_nested_name_for_resolver_to_reject(self) -> None:
        # parse does not itself sanitize; it hands the raw name to the resolver.
        assert uploads.parse_upload_url("/api/agents/7/uploads/../secret") == (7, "../secret")


class TestResolveUploadPath:
    def test_upload_dir_is_owner_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Upload directories repair a lax mode before files are read or written."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        directory = tmp_path / "Downloads" / "AvaAgent-7"
        directory.mkdir(parents=True)
        directory.chmod(0o755)

        assert uploads.agent_upload_dir(7) == directory
        assert directory.stat().st_mode & 0o777 == 0o700

    def test_resolves_inside_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        p = uploads.resolve_upload_path(7, "foo.png")
        assert p == (tmp_path / "Downloads" / "AvaAgent-7" / "foo.png").resolve()

    def test_rejects_parent_traversal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with pytest.raises(ValueError, match="escapes"):
            uploads.resolve_upload_path(7, "../../etc/passwd")

    def test_rejects_absolute(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        with pytest.raises(ValueError, match="escapes"):
            uploads.resolve_upload_path(7, "/etc/passwd")


class TestImageMime:
    def test_known_image_suffixes(self) -> None:
        assert uploads.image_mime_for("a.png") == "image/png"
        assert uploads.image_mime_for("a.JPG") == "image/jpeg"
        assert uploads.image_mime_for("a.webp") == "image/webp"

    def test_non_image_returns_none(self) -> None:
        assert uploads.image_mime_for("a.pdf") is None
        assert uploads.image_mime_for("a.txt") is None
        assert uploads.image_mime_for("noext") is None


class _FakeResp:
    def __init__(self, content: bytes, status: int = 200) -> None:
        self.content = content
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"{self.status_code}",
                request=None,  # type: ignore[arg-type]
                response=None,  # type: ignore[arg-type]
            )


class TestFetchUploadB64:
    def test_fetches_and_encodes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The image is fetched over HTTP from the gateway (its URL is the
        address — never the local disk), carrying the bearer header, and
        returned as base64."""
        from shared import http_dial

        raw = b"\x89PNG\r\n\x1a\n" + b"pixels"
        seen: dict[str, object] = {}

        def _fake_get(url: str, **kwargs: object) -> _FakeResp:
            seen["url"] = url
            seen["headers"] = kwargs.get("headers")
            return _FakeResp(raw)

        monkeypatch.setattr(http_dial, "get", _fake_get)
        monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw.test:8000")
        from shared.config import settings

        monkeypatch.setattr(settings.data_plane, "cluster_secret", "test-secret")
        mime, b64 = uploads.fetch_upload_b64(7, "shot.png")
        assert mime == "image/png"
        assert base64.standard_b64decode(b64) == raw
        assert seen["url"] == "http://gw.test:8000/api/agents/7/uploads/shot.png"
        assert seen["headers"] == {"Authorization": "Bearer test-secret"}  # cluster-secret bearer

    def test_non_image_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from shared import http_dial

        monkeypatch.setattr(http_dial, "get", lambda *_a, **_kw: pytest.fail("must not fetch"))  # pyright: ignore[reportUnknownArgumentType]
        with pytest.raises(ValueError, match="not a recognized image"):
            uploads.fetch_upload_b64(7, "notes.txt")

    def test_missing_upload_raises_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 404 (or any failed fetch) surfaces as OSError — the claim node's
        degrade-to-text-note path catches exactly that."""
        from shared import http_dial

        monkeypatch.setattr(http_dial, "get", lambda *_a, **_kw: _FakeResp(b"", status=404))  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw.test:8000")
        with pytest.raises(OSError, match="404"):
            uploads.fetch_upload_b64(7, "gone.png")


def test_upload_url_roundtrips() -> None:
    assert uploads.parse_upload_url(uploads.upload_url(3, "a.png")) == (3, "a.png")

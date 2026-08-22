"""Hermetic tests for the telegram-send-file skill script.

The skill's live behavior (an outward POST to api.telegram.org) never runs
here — real external sends require explicit user approval. These lock the
contract with a mocked httpx transport: the multipart payload shape, the
credential source, validation gates, and the token-hygiene error paths
(httpx errors must never surface the request URL, which carries the bot
token).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from shared.config import settings

_PATH = (
    Path(__file__).parents[2]
    / "ava_builtins"
    / "skills"
    / "telegram-send-file"
    / "scripts"
    / "send_file.py"
)
_spec = importlib.util.spec_from_file_location("telegram_send_file_under_test", _PATH)
assert _spec and _spec.loader
send_file = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = send_file
_spec.loader.exec_module(send_file)

FAKE_BOT_TOKEN = "123456:TEST-TOKEN"  # noqa: S105 - test fixture, never a real secret
OWNER = 123456789


def _credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the skill at the same config source IM Bridge uses.

    conftest pins the env aliases empty (leak guard), so the settings
    fallback path is what runs in tests — the same seam the im_bridge tests
    use. The env-first path is covered by the pure _credentials_from_env /
    injectable-env tests below.
    """
    monkeypatch.setattr(settings.telegram, "telegram_bot_token", FAKE_BOT_TOKEN)
    monkeypatch.setattr(settings.telegram, "telegram_owner_id", OWNER)


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# -- validation ------------------------------------------------------------


def test_validate_file_accepts_regular_file(tmp_path: Path) -> None:
    p = tmp_path / "report.pdf"
    p.write_bytes(b"x" * 100)
    assert send_file.validate_file(p) == p


def test_validate_file_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        send_file.validate_file(tmp_path / "nope.pdf")


def test_validate_file_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        send_file.validate_file(tmp_path)


def test_validate_file_empty_raises(tmp_path: Path) -> None:
    p = tmp_path / "empty.txt"
    p.write_bytes(b"")
    with pytest.raises(ValueError, match="empty"):
        send_file.validate_file(p)


def test_validate_file_over_50mb_raises(tmp_path: Path) -> None:
    p = tmp_path / "huge.bin"
    with p.open("wb") as fh:
        fh.seek(send_file.MAX_DOCUMENT_BYTES + 1)
        fh.write(b"x")
    with pytest.raises(ValueError, match="50 MB"):
        send_file.validate_file(p)


# -- send contract ---------------------------------------------------------


def test_send_document_posts_multipart_to_senddocument(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _credentials(monkeypatch)
    p = tmp_path / "report.txt"
    p.write_bytes(b"hello telegram")
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    with _client(handler) as client:
        result = send_file.send_document(
            token=FAKE_BOT_TOKEN, chat_id=OWNER, path=p, caption="Weekly", client=client
        )

    assert result["message_id"] == 42
    assert str(captured["url"]).endswith(f"/bot{FAKE_BOT_TOKEN}/sendDocument")
    body = captured["body"].decode("latin-1")
    assert 'name="chat_id"' in body and str(OWNER) in body
    assert 'name="caption"' in body and "Weekly" in body
    assert 'name="document"' in body and 'filename="report.txt"' in body
    assert b"hello telegram" in captured["body"]


def test_send_document_caption_optional(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _credentials(monkeypatch)
    p = tmp_path / "f.bin"
    p.write_bytes(b"data")
    body_seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body_seen["body"] = request.content
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    with _client(handler) as client:
        send_file.send_document(token=FAKE_BOT_TOKEN, chat_id=OWNER, path=p, client=client)
    assert b"caption" not in body_seen["body"]


def test_send_document_caption_too_long_raises(tmp_path: Path) -> None:
    p = tmp_path / "f.bin"
    p.write_bytes(b"data")
    with pytest.raises(ValueError, match="1024"):
        send_file.send_document(
            token=FAKE_BOT_TOKEN,
            chat_id=OWNER,
            path=p,
            caption="x" * (send_file.MAX_CAPTION_CHARS + 1),
        )


# -- error hygiene (the token must never leak) -----------------------------


def test_send_document_transport_error_never_leaks_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _credentials(monkeypatch)
    p = tmp_path / "f.bin"
    p.write_bytes(b"data")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with _client(handler) as client, pytest.raises(RuntimeError, match="ConnectError") as exc:
        send_file.send_document(token=FAKE_BOT_TOKEN, chat_id=OWNER, path=p, client=client)
    assert FAKE_BOT_TOKEN not in str(exc.value)


def test_send_document_http_error_keeps_status_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _credentials(monkeypatch)
    p = tmp_path / "f.bin"
    p.write_bytes(b"data")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="Bad Request: file is too big")

    with _client(handler) as client, pytest.raises(RuntimeError, match="HTTP 400") as exc:
        send_file.send_document(token=FAKE_BOT_TOKEN, chat_id=OWNER, path=p, client=client)
    assert "file is too big" in str(exc.value)
    assert FAKE_BOT_TOKEN not in str(exc.value)


def test_send_document_api_error_body_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _credentials(monkeypatch)
    p = tmp_path / "f.bin"
    p.write_bytes(b"data")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "chat not found"})

    with _client(handler) as client, pytest.raises(RuntimeError, match="chat not found"):
        send_file.send_document(token=FAKE_BOT_TOKEN, chat_id=OWNER, path=p, client=client)


# -- CLI wiring ------------------------------------------------------------


def test_main_missing_credentials_fails_before_sending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(settings.telegram, "telegram_bot_token", "")
    monkeypatch.setattr(settings.telegram, "telegram_owner_id", 0)
    p = tmp_path / "f.bin"
    p.write_bytes(b"data")
    assert send_file.main([str(p)]) == 1
    err = capsys.readouterr().err
    assert "AVA_TELEGRAM_BOT_TOKEN" in err


def test_main_missing_file_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert send_file.main([str(tmp_path / "nope.bin")]) == 1
    assert "not a file" in capsys.readouterr().err


def test_main_success_reports_message_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _credentials(monkeypatch)
    p = tmp_path / "report.pdf"
    p.write_bytes(b"x" * 10)
    calls: list[dict[str, Any]] = []

    def fake_send_document(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"message_id": 777}

    monkeypatch.setattr(send_file, "send_document", fake_send_document)
    assert send_file.main([str(p), "--caption", "hi"]) == 0
    out = capsys.readouterr().out
    assert "message_id=777" in out and "report.pdf" in out
    assert calls[0]["caption"] == "hi" and calls[0]["path"] == p


# -- credential resolution (env aliases vs settings fallback) -------------


def test_credentials_from_env_reads_aliases() -> None:
    env = {"AVA_TELEGRAM_BOT_TOKEN": "t", "AVA_TELEGRAM_OWNER_ID": "777"}
    assert send_file._credentials_from_env(env) == ("t", 777)


def test_credentials_from_env_malformed_owner_reads_zero() -> None:
    env = {"AVA_TELEGRAM_BOT_TOKEN": "t", "AVA_TELEGRAM_OWNER_ID": "not-a-number"}
    assert send_file._credentials_from_env(env) == ("t", 0)


def test_credentials_from_env_missing_reads_empty() -> None:
    assert send_file._credentials_from_env({}) == ("", 0)


def test_credentials_prefers_env_over_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.telegram, "telegram_bot_token", "settings:TOKEN")
    monkeypatch.setattr(settings.telegram, "telegram_owner_id", 888)
    env = {"AVA_TELEGRAM_BOT_TOKEN": "env:TOKEN", "AVA_TELEGRAM_OWNER_ID": "777"}
    assert send_file._credentials(env) == ("env:TOKEN", 777)


def test_credentials_falls_back_to_settings_when_env_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings.telegram, "telegram_bot_token", "settings:TOKEN")
    monkeypatch.setattr(settings.telegram, "telegram_owner_id", 888)
    assert send_file._credentials({"AVA_TELEGRAM_BOT_TOKEN": "", "AVA_TELEGRAM_OWNER_ID": "0"}) == (
        "settings:TOKEN",
        888,
    )

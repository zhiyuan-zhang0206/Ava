"""Native LGTM backend archive download: checksum refusal and bounded retries."""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.commands.observability import lgtm_native


def test_download_refuses_an_archive_with_the_wrong_sha256(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "native"

    def fake_download(_url: str, archive: Path) -> None:
        archive.write_bytes(b"untrusted")

    monkeypatch.setattr(lgtm_native, "download_with_retry", fake_download)

    with pytest.raises(RuntimeError, match="SHA256 mismatch"):
        lgtm_native.download_and_verify(
            "loki",
            "3.7.6",
            {"url": "https://example.invalid/loki.zip", "sha256": "0" * 64},
            destination,
            tag="darwin_arm64",
        )


def test_download_retry_preserves_sleep_and_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    retry_waits: list[float],
) -> None:
    calls = 0

    def fail_then_succeed(_url: str, _archive: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("bad payload")

    monkeypatch.setattr(lgtm_native, "stream_download", fail_then_succeed)
    monkeypatch.setattr(lgtm_native.time, "monotonic", lambda: 10.0)
    lgtm_native.download_with_retry("https://example.invalid/loki.zip", tmp_path / "loki.zip")

    assert calls == 2
    assert retry_waits == [5.0]
    assert capsys.readouterr().err == (
        "  ! lgtm native: download attempt 1/3 failed after 0s: bad payload\n"
    )


def test_download_retry_preserves_final_error_and_chain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    retry_waits: list[float],
) -> None:
    errors = [OSError(f"reset {i}") for i in range(1, 4)]

    def fail(_url: str, _archive: Path) -> None:
        raise errors.pop(0)

    final_error = errors[-1]
    monkeypatch.setattr(lgtm_native, "stream_download", fail)
    monkeypatch.setattr(lgtm_native.time, "monotonic", lambda: 10.0)
    url = "https://example.invalid/loki.zip"
    with pytest.raises(RuntimeError) as caught:
        lgtm_native.download_with_retry(url, tmp_path / "loki.zip")

    assert str(caught.value) == (
        f"failed to download native LGTM backend from {url} after 3 attempts (0s total): reset 3"
    )
    assert caught.value.__cause__ is final_error
    assert retry_waits == [5.0, 10.0]
    assert capsys.readouterr().err == "".join(
        f"  ! lgtm native: download attempt {i}/3 failed after 0s: reset {i}\n" for i in range(1, 4)
    )

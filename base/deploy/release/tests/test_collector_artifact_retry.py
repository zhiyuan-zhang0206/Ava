"""Collector download retry errors preserve timing and causal contracts."""

from pathlib import Path

import pytest

from base.deploy.release import collector_artifact as artifact


@pytest.mark.parametrize("failures", [1, 3])
def test_download_with_retry_preserves_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    retry_waits: list[float],
    failures: int,
) -> None:
    errors = [OSError(f"reset {i}") for i in range(1, failures + 1)]
    calls = 0

    def download(_url: str, dest: Path) -> None:
        nonlocal calls
        calls += 1
        if calls <= failures:
            raise errors[calls - 1]
        dest.write_bytes(b"ok")

    monkeypatch.setattr(artifact, "_stream_download", download)
    monkeypatch.setattr(artifact.time, "monotonic", lambda: 10.0)
    url = "https://example.invalid/t.tar.gz"
    if failures == 3:
        with pytest.raises(RuntimeError) as caught:
            artifact._download_with_retry(url, tmp_path / "t.tar.gz")
        assert (
            str(caught.value)
            == f"failed to download otel-collector from {url} after 3 attempts (0s total): reset 3"
        )
        assert caught.value.__cause__ is errors[-1]
    else:
        artifact._download_with_retry(url, tmp_path / "t.tar.gz")
        assert (tmp_path / "t.tar.gz").read_bytes() == b"ok"
    assert calls == min(failures + 1, 3)
    assert retry_waits == [5.0 * i for i in range(1, calls)]
    assert capsys.readouterr().err == "".join(
        f"  ! otel-collector: download attempt {i}/3 failed after 0s: reset {i}\n"
        for i in range(1, failures + 1)
    )

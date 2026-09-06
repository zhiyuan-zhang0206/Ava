"""Vendored relocatable Postgres resolution and hermetic extraction.

The real downloadable artifact is covered by ``scripts/pgvector_runtime_smoke.py``
in the ``backend-pgvector-smoke`` CI job. This unit suite stays offline while
covering the checksum, extraction, executable, and idempotence contracts that
``ava start`` depends on for a brew-free machine.
"""

from __future__ import annotations

import email.message
import hashlib
import io
import lzma
import subprocess
import tarfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

from shared import pg_tools
from shared import runtime_binaries as rb
from shared.config import settings


@pytest.fixture()
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the host-level runtime root at a tmp dir (via the cluster-registry
    anchor), so a test never touches the real ~/.ava/runtime."""
    monkeypatch.setattr(settings.general, "cluster_registry", str(tmp_path / "clusters.json"))


def test_pg_tool_prefers_vendored_dir(isolated_runtime: None) -> None:
    # Absent vendored tree -> pg_tool falls back to the host (brew/apt) path.
    assert "runtime" not in str(pg_tools.pg_tool("initdb"))

    # A present vendored tree wins.
    bin_dir = rb.vendored_pg_dir() / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "initdb").write_text("#!/bin/sh\n")
    assert pg_tools.pg_tool("initdb") == bin_dir / "initdb"
    assert rb.vendored_pg_bin_dir() == bin_dir


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://repo1.invalid/x.jar", status, "boom", email.message.Message(), None
    )


def _patch_urlopen(
    monkeypatch: pytest.MonkeyPatch, answers: list[urllib.error.URLError | _FakeResponse]
) -> tuple[list[str], list[float]]:
    """urlopen pops `answers` (an exception raises); returns (calls, recorded sleeps).
    The recorded call is the request's full URL (the code may pass headers)."""
    calls: list[str] = []
    sleeps: list[float] = []

    def fake_urlopen(url: urllib.request.Request, timeout: float) -> _FakeResponse:
        calls.append(url.full_url)
        answer = answers.pop(0)
        if isinstance(answer, urllib.error.URLError):
            raise answer
        return answer

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", sleeps.append)
    return calls, sleeps


def test_download_retries_transient_answers_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429/5xx from the mirror and socket-level failures back off and retry; the
    sha256 pin makes the eventual bytes trustworthy regardless of which attempt won."""
    calls, sleeps = _patch_urlopen(
        monkeypatch,
        [_http_error(429), _http_error(503), urllib.error.URLError("reset"), _FakeResponse(b"jar")],
    )
    assert rb._download("https://repo1.invalid/x.jar") == b"jar"
    assert len(calls) == 4
    assert sleeps == [2, 4, 8]


def test_download_fails_fast_on_a_permanent_http_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-transient 4xx (bad pin/artifact) is a permanent answer — no retry."""
    calls, sleeps = _patch_urlopen(monkeypatch, [_http_error(404)])
    with pytest.raises(RuntimeError, match="404"):
        rb._download("https://repo1.invalid/x.jar")
    assert len(calls) == 1
    assert sleeps == []


def test_download_gives_up_after_bounded_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, sleeps = _patch_urlopen(monkeypatch, [_http_error(429)] * rb._DOWNLOAD_ATTEMPTS)
    with pytest.raises(RuntimeError, match="429"):
        rb._download("https://repo1.invalid/x.jar")
    assert len(calls) == rb._DOWNLOAD_ATTEMPTS
    assert sleeps == [2, 4, 8]


def _synthetic_pg_jar(artifact_name: str) -> bytes:
    """A jar wrapping the minimal executable Postgres tree used by the test."""
    txz = io.BytesIO()
    with tarfile.open(fileobj=txz, mode="w:xz") as tar:
        for directory in ("bin", "lib", "share"):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)
        initdb = b'#!/bin/sh\necho "17.4"\n'
        info = tarfile.TarInfo("bin/initdb")
        info.mode = 0o755
        info.size = len(initdb)
        tar.addfile(info, io.BytesIO(initdb))

    jar = io.BytesIO()
    with zipfile.ZipFile(jar, mode="w") as archive:
        archive.writestr(f"{artifact_name}.txz", txz.getvalue())
    return jar.getvalue()


def test_ensure_pg_binaries_hermetic_fixture(
    isolated_runtime: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_name = "synthetic-postgres"
    synthetic_jar = _synthetic_pg_jar(artifact_name)
    key = rb._platform_key()
    monkeypatch.setitem(
        rb._PG_ARTIFACTS,
        key,
        (artifact_name, hashlib.sha256(synthetic_jar).hexdigest()),
    )
    downloads: list[str] = []

    def fake_download(url: str, *, headers: dict[str, str] | None = None) -> bytes:
        downloads.append(url)
        return synthetic_jar

    monkeypatch.setattr(rb, "_download", fake_download)
    assert rb.vendored_pg_bin_dir() is None

    bin_dir = rb.ensure_pg_binaries()
    assert (bin_dir / "initdb").exists()
    assert rb.vendored_pg_bin_dir() == bin_dir

    # The relocatable binary runs standalone (no brew, from its extracted path).
    out = subprocess.run(  # noqa: S603 — resolved vendored initdb path + static flag
        [str(bin_dir / "initdb"), "--version"], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "17.4"

    # Idempotent: a second ensure is a no-op returning the same dir (no re-download).
    assert rb.ensure_pg_binaries() == bin_dir
    assert len(downloads) == 1


# ── pgvector injection unit coverage ───────────────────────────────────────
# The real artifacts are covered by scripts/pgvector_runtime_smoke.py (the CI
# gate); these pin the extraction/injection mechanics with synthetic archives.


def _ar_member(name: str, payload: bytes) -> bytes:
    """One ar member: 60-byte header + payload, 2-byte aligned like real deb."""
    header = (f"{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(payload):<10}`\n").encode("ascii")
    padding = b"\n" if len(payload) % 2 else b""
    return header + payload + padding


def _synthetic_deb() -> bytes:
    """A minimal deb-shaped ar archive carrying data.tar.xz with the three
    pgvector files at the PGDG install paths for the pinned major. Members
    carry the `./` prefix like a real dpkg-deb data tar."""
    major = rb._PG_VERSION.split(".", 1)[0]
    payloads = {
        f"./usr/lib/postgresql/{major}/lib/vector.so": b"SO",
        f"./usr/share/postgresql/{major}/extension/vector.control": b"CTRL",
        f"./usr/share/postgresql/{major}/extension/{rb._PGVECTOR_SQL}": b"SQL",
    }
    data_tar = io.BytesIO()
    with tarfile.open(fileobj=data_tar, mode="w") as tar:
        for name, content in payloads.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    deb = b"!<arch>\n"
    deb += _ar_member("debian-binary", b"2.0\n")
    deb += _ar_member("control.tar.xz", lzma.compress(b""))
    deb += _ar_member("data.tar.xz", lzma.compress(data_tar.getvalue()))
    return deb


def _synthetic_bottle() -> bytes:
    """A minimal bottle-shaped gzipped tar with the pgvector members at the
    formula install paths for the pinned major."""
    major = rb._PG_VERSION.split(".", 1)[0]
    prefix = f"pgvector/{rb._PGVECTOR_VERSION}/"
    payloads = {
        f"{prefix}lib/postgresql@{major}/vector.dylib": b"DYLIB",
        f"{prefix}share/postgresql@{major}/extension/vector.control": b"CTRL",
        f"{prefix}share/postgresql@{major}/extension/{rb._PGVECTOR_SQL}": b"SQL",
    }
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tar:
        for name, content in payloads.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return blob.getvalue()


def test_pgvector_deb_extraction_layout() -> None:
    assert rb._pgvector_extension_files(_synthetic_deb(), "linux-x86_64") == {
        "lib/postgresql/vector.so": b"SO",
        "share/postgresql/extension/vector.control": b"CTRL",
        f"share/postgresql/extension/{rb._PGVECTOR_SQL}": b"SQL",
    }


def test_pgvector_bottle_extraction_layout() -> None:
    assert rb._pgvector_extension_files(_synthetic_bottle(), "darwin-arm64") == {
        "lib/postgresql/vector.dylib": b"DYLIB",
        "share/postgresql/extension/vector.control": b"CTRL",
        f"share/postgresql/extension/{rb._PGVECTOR_SQL}": b"SQL",
    }


def test_pgvector_extraction_fails_fast_on_missing_member() -> None:
    """A renamed upstream layout must fail loudly, never inject a partial set."""
    major = rb._PG_VERSION.split(".", 1)[0]
    data_tar = io.BytesIO()
    with tarfile.open(fileobj=data_tar, mode="w") as tar:
        info = tarfile.TarInfo(f"./usr/lib/postgresql/{major}/lib/vector.so")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"SO"))
    deb = (
        b"!<arch>\n"
        + _ar_member("debian-binary", b"2.0\n")
        + _ar_member("data.tar.xz", lzma.compress(data_tar.getvalue()))
    )
    with pytest.raises(RuntimeError, match="member missing"):
        rb._pgvector_extension_files(deb, "linux-x86_64")


def test_ensure_pgvector_injects_and_is_idempotent(
    isolated_runtime: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    (rb.vendored_pg_dir() / "bin").mkdir(parents=True)
    (rb.vendored_pg_dir() / "bin" / "initdb").write_text("#!/bin/sh\n")
    # The synthetic deb is this test's artifact, so the pin table is swapped for
    # one whose sha matches it — the pin check itself stays exercised.
    synthetic = _synthetic_deb()
    fake_url = "https://example.invalid/pgvector.deb"
    monkeypatch.setattr(
        rb,
        "_PGVECTOR_ARTIFACTS",
        {"linux-x86_64": (fake_url, hashlib.sha256(synthetic).hexdigest())},
    )
    calls: list[str] = []

    def fake_download(url: str, *, headers: dict[str, str] | None = None) -> bytes:
        calls.append(url)
        return synthetic

    monkeypatch.setattr(rb, "_download", fake_download)
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")

    rb.ensure_pgvector()
    pg_dir = rb.vendored_pg_dir()
    assert (pg_dir / "lib/postgresql/vector.so").read_bytes() == b"SO"
    assert (pg_dir / "share/postgresql/extension/vector.control").read_bytes() == b"CTRL"
    assert (pg_dir / "share/postgresql/extension" / rb._PGVECTOR_SQL).read_bytes() == b"SQL"
    assert calls == [fake_url]
    rb.ensure_pgvector()  # idempotent: detection files exist -> no second download
    assert len(calls) == 1
    # Detection covers BOTH files: deleting the module alone re-injects
    # (a partially deleted injection must not silently survive).
    (pg_dir / "lib/postgresql/vector.so").unlink()
    rb.ensure_pgvector()
    assert len(calls) == 2
    assert (pg_dir / "lib/postgresql/vector.so").read_bytes() == b"SO"


def test_ensure_pgvector_fails_on_checksum_mismatch(
    isolated_runtime: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    (rb.vendored_pg_dir() / "bin" / "initdb").parent.mkdir(parents=True)
    (rb.vendored_pg_dir() / "bin" / "initdb").write_text("#!/bin/sh\n")

    def fake_download(url: str, *, headers: dict[str, str] | None = None) -> bytes:
        return b"junk"

    monkeypatch.setattr(rb, "_download", fake_download)
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        rb.ensure_pgvector()


def test_ensure_pgvector_requires_pg_tree(isolated_runtime: None) -> None:
    with pytest.raises(RuntimeError, match="ensure_pg_binaries"):
        rb.ensure_pgvector()


def test_download_ghcr_blob_fetches_token_then_blob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str] | None]] = []

    def fake_download(url: str, *, headers: dict[str, str] | None = None) -> bytes:
        calls.append((url, headers))
        return b'{"token": "tok"}' if "token" in url else b"blob"

    monkeypatch.setattr(rb, "_download", fake_download)
    blob_url = "https://ghcr.io/v2/homebrew/core/pgvector/blobs/sha256:abc"
    assert rb._download_ghcr_blob(blob_url) == b"blob"
    assert calls[0] == (
        "https://ghcr.io/token?service=ghcr.io&scope=repository:homebrew/core/pgvector:pull",
        None,
    )
    assert calls[1] == (blob_url, {"Authorization": "Bearer tok"})


def test_pgvector_platform_key_out_of_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("platform.machine", lambda: "aarch64")
    with pytest.raises(RuntimeError, match="linux/aarch64"):
        rb._pgvector_platform_key()

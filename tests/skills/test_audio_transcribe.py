"""Hermetic unit tests for the audio-transcribe skill (no network, no ffmpeg).

The real OpenAI + ffmpeg path was verified live by hand during the build
(single-chunk and forced multi-chunk segmentation). These lock the pure logic:
source resolution, the 25 MB chunk-or-not decision + segmentation, and the
transcribe() orchestration/join — by faking the `_run` subprocess and the
OpenAI client.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_MOD = Path(__file__).parents[2] / "ava_builtins" / "skills" / "audio-transcribe" / "transcribe.py"
_spec = importlib.util.spec_from_file_location("audio_transcribe_under_test", _MOD)
assert _spec and _spec.loader
T = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = T
_spec.loader.exec_module(T)


def _cp(cmd: list[str], stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")


def _fake_run(*, compact_size: int, duration: str = "190.0", part_count: int = 0):
    """A fake `_run` that simulates ffmpeg/ffprobe/yt-dlp by creating the output
    files the real commands would and answering ffprobe with a duration."""

    def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        prog = cmd[0]
        if prog == "ffprobe":
            return _cp(cmd, stdout=duration)
        if prog == "yt-dlp":
            Path(str(cmd[cmd.index("-o") + 1]).replace("%(ext)s", "mp3")).write_bytes(b"x")
            return _cp(cmd)
        if prog == "ffmpeg":
            if "segment" in cmd:  # split: create part files next to the template
                tmpl = Path(cmd[-1])
                for i in range(part_count):
                    Path(str(tmpl).replace("%03d", f"{i:03d}")).write_bytes(b"p")
            else:  # transcode: last arg is compact.mp3
                Path(cmd[-1]).write_bytes(b"c" * compact_size)
            return _cp(cmd)
        return _cp(cmd)

    return run


# --------------------------------------------------------------------------- #
# source resolution
# --------------------------------------------------------------------------- #


def test_resolve_local_file_passthrough(tmp_path: Path) -> None:
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    assert T._resolve_to_audio(str(f), tmp_path) == f


def test_resolve_bad_source_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        T._resolve_to_audio("not-a-file-or-url", tmp_path)


def test_resolve_video_id_downloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(T, "_run", _fake_run(compact_size=1))
    got = T._resolve_to_audio("dRsjO-88nBs", tmp_path)
    assert got.exists() and got.name == "src.mp3"


# --------------------------------------------------------------------------- #
# chunking
# --------------------------------------------------------------------------- #


def test_prepare_single_chunk_when_small(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(T, "_run", _fake_run(compact_size=1000, duration="120.0"))
    chunks = T._prepare_chunks(tmp_path / "in.mp3", tmp_path, max_bytes=10_000, max_segment_s=900)
    assert [c.name for c in chunks] == ["compact.mp3"]


def test_prepare_segments_when_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(T, "_run", _fake_run(compact_size=50_000, duration="190.0", part_count=3))
    chunks = T._prepare_chunks(tmp_path / "in.mp3", tmp_path, max_bytes=20_000, max_segment_s=900)
    assert [c.name for c in chunks] == ["part_000.mp3", "part_001.mp3", "part_002.mp3"]


def test_prepare_segments_on_duration_even_when_small(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Under the byte cap but over the duration ceiling -> still segments (a long
    # podcast that compresses small must not be sent as one over-long request).
    monkeypatch.setattr(T, "_run", _fake_run(compact_size=1000, duration="3600.0", part_count=4))
    chunks = T._prepare_chunks(tmp_path / "in.mp3", tmp_path, max_bytes=10_000, max_segment_s=900)
    assert len(chunks) == 4


def test_prepare_raises_on_oversize_part(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(T, "_run", _fake_run(compact_size=50_000, duration="190.0", part_count=2))
    monkeypatch.setattr(T, "_OPENAI_MAX_BYTES", 0)  # any part (>=1 byte) now exceeds the cap
    with pytest.raises(RuntimeError, match="25 MB API cap"):
        T._prepare_chunks(tmp_path / "in.mp3", tmp_path, max_bytes=20_000, max_segment_s=900)


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #


def test_transcribe_joins_chunk_texts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(T, "_resolve_to_audio", lambda src, wd: tmp_path / "m.mp3")  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        T,
        "_prepare_chunks",
        lambda m, wd, mb, ms: [tmp_path / "p0.mp3", tmp_path / "p1.mp3"],  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(T, "_audio_seconds", lambda m: 12.3)  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    seen: list[Path] = []

    def fake_tx(path: Path, *, model: str, language: str | None, prompt: str | None) -> str:
        seen.append(path)
        return f"text-of-{path.name}"

    monkeypatch.setattr(T, "_transcribe_file", fake_tx)
    out = T.transcribe("whatever", model="gpt-4o-transcribe")
    assert out["text"] == "text-of-p0.mp3\ntext-of-p1.mp3"
    assert out["chunks"] == 2
    assert out["audio_seconds"] == 12.3
    assert len(seen) == 2  # one API call per chunk, in order


def test_transcribe_rejects_oversize_max_bytes() -> None:
    with pytest.raises(ValueError, match="25 MB"):
        T.transcribe("x", max_bytes=99 * 1024 * 1024)


def test_transcribe_rejects_over_duration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(T, "_resolve_to_audio", lambda src, wd: tmp_path / "m.mp3")  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(T, "_audio_seconds", lambda m: 10_000.0)  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(ValueError, match="max_duration_s"):
        T.transcribe("x", max_duration_s=3600)

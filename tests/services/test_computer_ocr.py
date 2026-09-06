"""Unit tests for services/computer/ocr.py — the Vision-based screen OCR.

Covers the TSV parsing, coordinate passthrough (the Swift binary already
emits physical pixels, top-left origin), soft-failure surface (OcrError on
build/run problems), and the build-on-demand binary cache. The Swift
recognition itself is exercised live on the preview cluster, not in unit
tests (same stance as the permissions-helper tests).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from services.computer import ocr as ocr_mod
from services.computer.ocr import OcrError, ocr_image


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A prebuilt binary, older than the real ocr.swift, so _binary() runs
    the OCR subprocess without recompiling. Lives in the test's own tmp dir,
    never a fixed /tmp path (audit M-3)."""
    bin_path = tmp_path / "fake-ocr-bin" / "ocr"
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    bin_path.write_text("fake")
    os.utime(bin_path, (time.time(), time.time()))
    monkeypatch.setattr(ocr_mod, "_BIN", bin_path)
    return bin_path


def _stub_run(monkeypatch: pytest.MonkeyPatch, results: list[_FakeCompleted]) -> None:
    def _run(cmd: list[str], **kw: Any) -> _FakeCompleted:
        return results.pop(0) if results else _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", _run)


def _force_rebuild(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str) -> None:
    """Make _binary() see a stale binary + newer source (the swiftc path).

    The fake binary/source live under the test's own tmp dir (audit M-3),
    keyed by `name` so one test's rebuild state never touches another's."""
    bin_path = tmp_path / name / "ocr"
    src_path = tmp_path / name / "s.swift"
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    bin_path.write_text("old")
    src_path.parent.mkdir(parents=True, exist_ok=True)
    src_path.write_text("// source")
    future = time.time() + 7200
    os.utime(src_path, (future, future))
    monkeypatch.setattr(ocr_mod, "_SOURCE", src_path)
    monkeypatch.setattr(ocr_mod, "_BIN", bin_path)


def test_parses_tsv_lines_zh_en(fake_bin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = "\u4f60\u597d\u4e16\u754c\t10.0\t20.0\t100.0\t30.0\nHello\t200.5\t400.25\t80.0\t20.0\n"
    _stub_run(monkeypatch, [_FakeCompleted(stdout=out)])
    items = ocr_image("/tmp/x.png")  # noqa: S108
    assert items == [
        {"text": "\u4f60\u597d\u4e16\u754c", "x": 10.0, "y": 20.0, "w": 100.0, "h": 30.0},
        {"text": "Hello", "x": 200.5, "y": 400.25, "w": 80.0, "h": 20.0},
    ]


def test_skips_malformed_lines(fake_bin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = "short\t1.0\nbad\tx\ty\tw\th\nok\t1\t2\t3\t4\n"
    _stub_run(monkeypatch, [_FakeCompleted(stdout=out)])
    items = ocr_image("/tmp/x.png")  # noqa: S108
    assert items == [{"text": "ok", "x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0}]


def test_nonzero_exit_raises(fake_bin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, [_FakeCompleted(returncode=1, stderr="boom")])
    with pytest.raises(OcrError, match="boom"):
        ocr_image("/tmp/x.png")  # noqa: S108


def test_timeout_raises(fake_bin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _hang(cmd: list[str], **kw: Any) -> _FakeCompleted:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    monkeypatch.setattr(subprocess, "run", _hang)
    with pytest.raises(OcrError, match="timed out"):
        ocr_image("/tmp/x.png")  # noqa: S108


def test_empty_output_is_empty_list(fake_bin: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, [_FakeCompleted(stdout="")])
    assert ocr_image("/tmp/x.png") == []  # noqa: S108


def test_missing_swiftc_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_rebuild(monkeypatch, tmp_path, "rebuild1")
    monkeypatch.setattr(ocr_mod.shutil, "which", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(OcrError, match="swiftc"):
        ocr_image("/tmp/x.png")  # noqa: S108


def test_swiftc_failure_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_rebuild(monkeypatch, tmp_path, "rebuild2")
    monkeypatch.setattr(ocr_mod.shutil, "which", lambda _name: "/usr/bin/swiftc")  # pyright: ignore[reportUnknownArgumentType]

    def _run(cmd: list[str], **kw: Any) -> _FakeCompleted:
        return _FakeCompleted(returncode=1, stderr="compile error")

    monkeypatch.setattr(subprocess, "run", _run)
    with pytest.raises(OcrError, match="compile error"):
        ocr_image("/tmp/x.png")  # noqa: S108


def test_existing_fresh_binary_skips_compile(
    fake_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def _run(cmd: list[str], **kw: Any) -> _FakeCompleted:
        calls.append(cmd)
        return _FakeCompleted(stdout="ok\t1\t2\t3\t4\n")

    monkeypatch.setattr(subprocess, "run", _run)
    items = ocr_image("/tmp/x.png")  # noqa: S108
    assert items == [{"text": "ok", "x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0}]
    # exactly one subprocess call: the OCR run itself, no swiftc compile
    assert len(calls) == 1
    assert calls[0][0].endswith("ocr")

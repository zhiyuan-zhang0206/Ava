"""Screen-text OCR for the computer-use daemon (Task #1101 Phase 2).

`snapshot(include_ocr=true)` returns recognized text with physical-pixel
bounding boxes — the same coordinate space as click, so a caller clicks
exactly where it read a word.

Recognition runs in a tiny Swift binary compiled on demand from
`ocr.swift` (the same approach as the wechat-ocr skill): Vision's
VNRecognizeTextRequest with zh-Hans + en, emitting TSV lines
`text\tx\ty\tw\th` in physical pixels, top-left origin. The binary lives
under $AVA_HOME/logs/computer/ocr-bin/ and is rebuilt whenever the source is
newer (swiftc is not invoked per call).

OCR reads a PNG file and needs no TCC grant: the screen-recording
authorization already lives in the permissions helper, which produced the
PNG. Failure is soft by design — snapshot stays usable without text
recognition (the daemon surfaces `ocr: []` + `ocr_error` and carries on).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from shared.paths import logs_dir

_SOURCE = Path(__file__).with_name("ocr.swift")
_BIN_DIR = logs_dir() / "computer" / "ocr-bin"
_BIN = _BIN_DIR / "ocr"
# A 4K full-screen run takes ~0.5-2s; 30s is generous but bounded.
_OCR_TIMEOUT_S = 30.0


class OcrError(Exception):
    """OCR could not run or produced no parseable output."""


def _binary() -> Path:
    """The compiled OCR binary, built on demand (source-newer check)."""
    if not _BIN.exists() or _SOURCE.stat().st_mtime > _BIN.stat().st_mtime:
        _BIN_DIR.mkdir(parents=True, exist_ok=True)
        swiftc = shutil.which("swiftc")
        if swiftc is None:
            raise OcrError("swiftc not found on PATH — cannot build the OCR helper")
        built = subprocess.run(  # noqa: S603 — swiftc from PATH, argv is our own source
            [swiftc, "-o", str(_BIN), str(_SOURCE)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if built.returncode != 0:
            raise OcrError(f"swiftc failed: {built.stderr.strip()[:300]}")
    return _BIN


def ocr_image(path: str | Path) -> list[dict[str, float | str]]:
    """Recognize text in a PNG and return items with physical-pixel boxes.

    Each item is {"text", "x", "y", "w", "h"} in the same coordinate space
    as the daemon's click tool (top-left origin). Raises OcrError when the
    binary cannot build, the run fails, or the output is unparseable — the
    daemon degrades a failed OCR to `ocr: []` + `ocr_error`, never failing
    the snapshot itself.
    """
    try:
        ran = subprocess.run(  # noqa: S603 — our own compiled binary, caller path
            [str(_binary()), str(path)],
            capture_output=True,
            text=True,
            timeout=_OCR_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise OcrError(f"ocr run failed: {e}") from e
    if ran.returncode != 0:
        raise OcrError(f"ocr exited {ran.returncode}: {ran.stderr.strip()[:300]}")
    items: list[dict[str, float | str]] = []
    for line in ran.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 5:
            try:
                items.append(
                    {
                        "text": parts[0].strip(),
                        "x": float(parts[1]),
                        "y": float(parts[2]),
                        "w": float(parts[3]),
                        "h": float(parts[4]),
                    }
                )
            except ValueError:
                continue
    return items

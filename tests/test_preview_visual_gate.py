"""Unit contracts for the CI preview-gate golden minting helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from playwright.sync_api import Browser

from scripts.post_deploy_visual_check import _expected_capture_names
from tests.e2e.test_preview_visual_gate import (
    _crop_results,
    _mint_goldens,
    _structural_failures,
)


class _FakeBrowser:
    version = "147.0.0.0-fake"


def _write_captures(captures: Path, names: set[str] | None = None) -> None:
    captures.mkdir(parents=True)
    for name in names if names is not None else _expected_capture_names():
        (captures / name).write_bytes(b"png")


def test_mint_goldens_copies_all_captures_and_writes_provenance(tmp_path: Path) -> None:
    captures = tmp_path / "captures"
    _write_captures(captures)
    golden_root = tmp_path / "preview-gate"

    _mint_goldens(cast(Browser, _FakeBrowser()), [], captures, golden_root=golden_root)

    golden_dir = golden_root / "captures"
    expected = {name.replace("-current-", "-golden-") for name in _expected_capture_names()}
    assert {path.name for path in golden_dir.glob("*-golden-[12].png")} == expected
    meta = json.loads((golden_root / "meta.json").read_text())
    assert meta["browser_engine"] == "playwright-chromium 147.0.0.0-fake"


def test_mint_goldens_refuses_a_structurally_broken_run(tmp_path: Path) -> None:
    captures = tmp_path / "captures"
    _write_captures(captures)

    with pytest.raises(AssertionError, match="structurally broken"):
        _mint_goldens(
            cast(Browser, _FakeBrowser()),
            [{"kind": "visible-panel", "selector": "form"}],
            captures,
            golden_root=tmp_path / "preview-gate",
        )


def test_mint_goldens_refuses_an_incomplete_capture_set(tmp_path: Path) -> None:
    captures = tmp_path / "captures"
    _write_captures(captures, {next(iter(_expected_capture_names()))})

    with pytest.raises(AssertionError, match="incomplete capture set"):
        _mint_goldens(
            cast(Browser, _FakeBrowser()), [], captures, golden_root=tmp_path / "preview-gate"
        )


def test_result_flatteners_keep_surface_viewport_theme_context() -> None:
    combinations: list[dict[str, object]] = [
        {
            "surface": "login",
            "viewport": "desktop",
            "theme": "light",
            "structural_failures": [{"kind": "runner-error", "selector": "document"}],
            "pixel_diffs": [
                {"surface": "login-card", "status": "drift", "drifted": True},
                {"surface": "login-card", "status": "match", "drifted": False},
            ],
        }
    ]

    structural = _structural_failures(combinations)
    assert structural[0]["surface"] == "login"
    assert structural[0]["viewport"] == "desktop"
    assert structural[0]["theme"] == "light"

    crops = _crop_results(combinations)
    assert len(crops) == 2
    assert [crop["status"] for crop in crops] == ["drift", "match"]

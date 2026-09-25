"""Repeat start observes a generation before changing any shared launch input."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cli.commands._start_generation import configuration_digest, launch_digest, source_digest


def test_development_generation_includes_dirty_and_untracked_source(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603 — test-owned repository
    source = tmp_path / "app.py"
    source.write_text("value = 1\n")
    (tmp_path / ".gitignore").write_text("ignored/\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)  # noqa: S603 — test-owned repository
    subprocess.run(  # noqa: S603 — test-owned repository and explicit local identity
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "source fixture",
        ],
        check=True,
    )
    initial = source_digest(tmp_path)
    source.write_text("value = 2\n")
    assert source_digest(tmp_path) != initial
    source.write_text("value = 1\n")
    assert source_digest(tmp_path) == initial
    added = tmp_path / "new.py"
    added.write_text("pass\n")
    assert source_digest(tmp_path) != initial
    added.unlink()
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "build").write_text("generated")
    assert source_digest(tmp_path) == initial
    assert launch_digest(tmp_path, {"PRIVATE": "first"}, home=tmp_path) != launch_digest(
        tmp_path, {"PRIVATE": "second"}, home=tmp_path
    )
    home = tmp_path / "ignored" / "home"
    home.mkdir()
    (home / ".env").write_text("AVA_SSE_THROTTLE_RATE=10\n")
    configured = launch_digest(tmp_path, {}, home=home)
    (home / ".env").write_text("AVA_SSE_THROTTLE_RATE=20\n")
    assert source_digest(tmp_path) == initial
    assert launch_digest(tmp_path, {}, home=home) != configured
    source.unlink()
    assert source_digest(tmp_path) != initial


def test_unreadable_source_inventory_refuses(tmp_path: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        source_digest(tmp_path)


def test_generation_binds_configuration_omitted_from_transport(tmp_path: Path) -> None:
    environment = tmp_path / ".env"
    environment.write_text("AVA_SSE_THROTTLE_RATE=10\n")
    initial = configuration_digest(tmp_path)
    environment.write_text("AVA_SSE_THROTTLE_RATE=20\n")
    assert configuration_digest(tmp_path) != initial
    environment.write_text("AVA_SSE_THROTTLE_RATE=10\n")
    assert configuration_digest(tmp_path) == initial
    plugin = tmp_path / "configs" / "example" / "config.json"
    plugin.parent.mkdir(parents=True)
    plugin.write_text('{"option":true}')
    configured = configuration_digest(tmp_path)
    assert configured != initial
    plugin.write_text('{"option":false}')
    assert configuration_digest(tmp_path) != configured
    plugin.unlink()
    assert configuration_digest(tmp_path) == initial


def test_live_admission_requires_positive_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.commands import _root_driver as driver

    def no_status(_client: object) -> None:
        return None

    monkeypatch.setattr(driver, "_root_status", no_status)
    monkeypatch.setattr(driver, "_root_client", object)

    def unknown() -> None:
        raise RuntimeError("retained child custody")

    monkeypatch.setattr(driver, "_require_root_absent", unknown)
    with pytest.raises(RuntimeError, match="retained child custody"):
        driver.admit_live_start((), Path("/unread-source"), frozenset({"gateway"}), reconcile=True)

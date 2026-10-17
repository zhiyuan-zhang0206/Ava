from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from scripts.codegen import build_android_release as release


def _apk(directory: Path) -> Path:
    apk = directory / "meiva-app-0.4.2.apk"
    apk.write_bytes(b"release apk")
    return apk


def test_version_code_adds_the_run_number_to_the_base() -> None:
    assert release.build_version_code(1) == release.BUILD_NUMBER_BASE + 1
    assert release.build_version_code(42) == release.BUILD_NUMBER_BASE + 42


def test_version_code_stays_above_the_semver_derived_codes() -> None:
    assert release.build_version_code(1) > release.LEGACY_VERSION_CODE


def test_version_code_increases_strictly_with_the_run_number() -> None:
    codes = [release.build_version_code(run) for run in (1, 2, 3, 100)]
    assert codes == sorted(set(codes))


@pytest.mark.parametrize("run_number", [0, -1])
def test_version_code_rejects_a_missing_run_number(run_number: int) -> None:
    with pytest.raises(ValueError, match="run number must be positive"):
        release.build_version_code(run_number)


def test_tauri_config_pins_the_identity_and_disables_auto_increment() -> None:
    overlay = json.loads(release.tauri_config(version_name="0.4.2", version_code=1_000_007))

    assert overlay == {
        "version": "0.4.2",
        "bundle": {
            "android": {
                "versionCode": 1_000_007,
                "autoIncrementVersionCode": False,
            }
        },
    }


def test_manifest_describes_the_staged_apk(tmp_path: Path) -> None:
    apk = _apk(tmp_path)

    manifest = release.build_manifest(
        version_code=1_000_007,
        version_name="0.4.2",
        apk=apk,
        commit="a" * 40,
        repo="owner/Meiva",
        built_at="2026-10-17T03:04:05+00:00",
    )

    assert manifest == {
        "versionCode": 1_000_007,
        "versionName": "0.4.2",
        "apkUrl": "https://github.com/owner/Meiva/releases/download/android-latest/meiva-app-0.4.2.apk",
        "sha256": hashlib.sha256(b"release apk").hexdigest(),
        "commit": "a" * 40,
        "builtAt": "2026-10-17T03:04:05Z",
    }


def test_manifest_quotes_the_asset_name_in_the_url(tmp_path: Path) -> None:
    apk = tmp_path / "meiva-app-1.0.0+build.7.apk"
    apk.write_bytes(b"apk")

    manifest = release.build_manifest(
        version_code=1_000_008,
        version_name="1.0.0+build.7",
        apk=apk,
        commit="b" * 40,
        repo="owner/Meiva",
        built_at="2026-10-17T03:04:05Z",
    )

    assert manifest["apkUrl"].endswith("/meiva-app-1.0.0%2Bbuild.7.apk")


def test_manifest_defaults_built_at_to_utc_now(tmp_path: Path) -> None:
    manifest = release.build_manifest(
        version_code=1_000_009,
        version_name="0.4.2",
        apk=_apk(tmp_path),
        commit="c" * 40,
        repo="owner/Meiva",
    )

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", manifest["builtAt"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("commit", "abc"),
        ("commit", "A" * 40),
        ("repo", "owner"),
        ("repo", "owner/Meiva/extra"),
        ("version_name", "0.4 2"),
        ("version_name", ""),
    ],
)
def test_manifest_rejects_identity_values_it_cannot_publish(
    tmp_path: Path, field: str, value: str
) -> None:
    arguments: dict[str, Any] = {
        "version_code": 1_000_010,
        "version_name": "0.4.2",
        "apk": _apk(tmp_path),
        "commit": "d" * 40,
        "repo": "owner/Meiva",
        "built_at": "2026-10-17T03:04:05Z",
    }
    arguments[field] = value

    with pytest.raises(ValueError):
        release.build_manifest(**arguments)


def test_manifest_requires_a_built_at_offset(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="UTC offset"):
        release.build_manifest(
            version_code=1_000_011,
            version_name="0.4.2",
            apk=_apk(tmp_path),
            commit="e" * 40,
            repo="owner/Meiva",
            built_at="2026-10-17T03:04:05",
        )


def _previous_manifest(directory: Path, version_code: int) -> Path:
    path = directory / "android-latest.json"
    path.write_text(json.dumps({"versionCode": version_code}), encoding="utf-8")
    return path


def test_guard_accepts_a_newer_code(tmp_path: Path) -> None:
    release.assert_version_code_advances(
        current=_previous_manifest(tmp_path, 1_000_016), version_code=1_000_017
    )


@pytest.mark.parametrize("previous", [1_000_017, 1_000_018])
def test_guard_refuses_a_code_that_does_not_advance(tmp_path: Path, previous: int) -> None:
    with pytest.raises(ValueError, match="already advertises"):
        release.assert_version_code_advances(
            current=_previous_manifest(tmp_path, previous), version_code=1_000_017
        )


@pytest.mark.parametrize("body", [{}, {"versionCode": "1000017"}, {"versionCode": True}])
def test_guard_rejects_a_manifest_without_an_integer_code(
    tmp_path: Path, body: dict[str, object]
) -> None:
    current = tmp_path / "android-latest.json"
    current.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(TypeError, match="no integer versionCode"):
        release.assert_version_code_advances(current=current, version_code=1_000_017)


def test_cli_writes_the_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    apk = _apk(tmp_path)
    out = tmp_path / "android-latest.json"

    exit_code = release.main(
        [
            "manifest",
            "--version-code",
            "1000012",
            "--version-name",
            "0.4.2",
            "--apk",
            str(apk),
            "--commit",
            "f" * 40,
            "--repo",
            "owner/Meiva",
            "--built-at",
            "2026-10-17T03:04:05Z",
            "--out",
            str(out),
        ]
    )

    assert exit_code == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["versionCode"] == 1_000_012
    assert capsys.readouterr().out == out.read_text(encoding="utf-8")

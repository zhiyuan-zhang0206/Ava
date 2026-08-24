"""App-CI policy checks that do not need a platform toolchain."""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci-app.yml"


def _workflow() -> str:
    return _WORKFLOW.read_text()


def test_desktop_matrix_compiles_on_every_supported_host_platform() -> None:
    text = _workflow()
    assert "macos-14" in text
    assert "windows-2022" in text


def test_android_job_builds_an_apk_after_applying_the_overlay() -> None:
    text = _workflow()
    assert "python3 ui/app/android/apply_overlay.py" in text
    assert "cargo tauri android build --apk" in text


def test_tauri_cli_is_exactly_pinned() -> None:
    text = _workflow()
    assert "TAURI_CLI_VERSION: '2.11.4'" in text
    assert "TAURI_CLI_VERSION: '^" not in text

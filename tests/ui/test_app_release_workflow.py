"""Release-app policy checks that do not need a platform toolchain."""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release-app.yml"


def _workflow() -> str:
    return _WORKFLOW.read_text()


def test_tauri_cli_is_exactly_pinned() -> None:
    text = _workflow()
    assert "TAURI_CLI_VERSION: '2.11.4'" in text
    assert "TAURI_CLI_VERSION: '^" not in text


def test_tag_releases_fail_closed_without_every_platform_signature() -> None:
    text = _workflow()
    assert 'if [ "$EVENT_NAME" = "push" ]; then' in text
    assert "Release tags require updater signing" in text
    assert "Release tags require Apple signing and notarization" in text
    assert "Release tags require Windows code-signing" in text


def test_android_release_workflow_is_separate_and_fails_closed() -> None:
    """Android publishing lives in release-android.yml (android-v* tags); its
    tag path must still fail closed without the Android signing secrets."""
    android_workflow = _REPO_ROOT / ".github" / "workflows" / "release-android.yml"
    text = android_workflow.read_text()
    assert "android-v[0-9]+.[0-9]+.[0-9]+" in text
    assert "Release tags require Android signing" in text


def test_manual_dispatch_keeps_the_explicit_unsigned_build_path() -> None:
    text = _workflow()
    assert "workflow_dispatch:" in text
    assert "EXTRA+=(--no-sign)" in text

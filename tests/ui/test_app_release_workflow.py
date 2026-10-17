from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release-app.yml"
_ANDROID_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release-android.yml"


def _workflow() -> str:
    return _WORKFLOW.read_text()


def _android_workflow() -> str:
    return _ANDROID_WORKFLOW.read_text()


def test_tauri_cli_is_exactly_pinned() -> None:
    text = _workflow()
    assert "TAURI_CLI_VERSION: '2.11.4'" in text
    assert "TAURI_CLI_VERSION: '^" not in text


def test_tag_releases_fail_closed_without_every_platform_signature() -> None:
    text = _workflow()
    assert 'if [ "$EVENT_NAME" = "push" ]; then' in text
    assert "Release tags require updater signing" in text
    assert "Release tags require Apple signing and notarization" in text


def test_android_release_workflow_is_separate_and_fails_closed() -> None:
    text = _android_workflow()
    assert "android-v[0-9]+.[0-9]+.[0-9]+" in text
    assert "Release tags require Android signing" in text


def test_android_build_injects_the_run_number_as_version_code() -> None:
    text = _android_workflow()
    assert "build_android_release.py version-code" in text
    assert "build_number: ${{ steps.build.outputs.build_number }}" in text
    assert "tauri-config" in text
    assert '--version-name "$APP_VERSION" --version-code "$BUILD_NUMBER"' in text
    assert "versionCode='$BUILD_NUMBER'" in text


def test_android_latest_publishes_the_manifest_with_the_apk() -> None:
    text = _android_workflow()
    assert "build_android_release.py manifest" in text
    assert "--out release-assets/android-latest.json" in text
    assert "assets=(release-assets/*.apk)" in text
    assert 'gh release upload "$TAG" "${assets[@]}" --clobber' in text


def test_android_manual_dispatch_builds_without_publishing_by_default() -> None:
    text = _android_workflow()
    assert "dry_run:" in text
    assert "default: true" in text
    assert "github.event_name != 'workflow_dispatch' || inputs.dry_run != true" in text


def test_android_publish_refuses_to_roll_the_channel_back() -> None:
    text = _android_workflow()
    assert "gh release download android-latest --pattern android-latest.json" in text
    assert "build_android_release.py assert-advances" in text


def test_manual_dispatch_keeps_the_explicit_unsigned_build_path() -> None:
    text = _workflow()
    assert "workflow_dispatch:" in text
    assert "EXTRA+=(--no-sign)" in text

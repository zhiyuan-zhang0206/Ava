"""Real uv transport checks: immutable pins, host settings, and editable identity."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from cli import _python_index
from tests.cli._python_install_fixture import PythonMirror, build_python_mirror


@pytest.fixture
def python_mirror(tmp_path: Path) -> Iterator[PythonMirror]:
    yield from build_python_mirror(tmp_path)


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, str]]:
    monkeypatch.setattr(_python_index.sys, "platform", "linux")
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"HOME": str(tmp_path / "home"), "XDG_CONFIG_DIRS": str(tmp_path / "global")}
    return repo, env


def _pip_file(env: dict[str, str], content: str) -> Path:
    path = Path(env["HOME"]) / ".config/pip/pip.conf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_machine_pip_index_is_reused_without_rewriting_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, env = _settings(tmp_path, monkeypatch)
    path = _pip_file(env, "[global]\nindex-url = https://mirror.example/simple\n")
    original = path.read_bytes()
    assert _python_index.python_index(repo, env) == "https://mirror.example/simple"
    assert path.read_bytes() == original


def test_uv_profile_and_pip_environment_precede_pip_machine_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, env = _settings(tmp_path, monkeypatch)
    _pip_file(env, "[global]\nindex-url = https://machine.example/simple\n")
    env["PIP_INDEX_URL"] = "https://pip-env.example/simple"
    assert _python_index.python_index(repo, env) == env["PIP_INDEX_URL"]
    env["UV_DEFAULT_INDEX"] = "https://pypi.org/simple"
    assert _python_index.python_index(repo, env) == "https://pypi.org/simple"


def test_native_uv_config_precedes_pip_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, env = _settings(tmp_path, monkeypatch)
    _pip_file(env, "[global]\nindex-url = https://pip.example/simple\n")
    path = Path(env["HOME"]) / ".config/uv/uv.toml"
    path.parent.mkdir(parents=True)
    path.write_text('[[index]]\nurl = "https://uv.example/simple"\ndefault = true\n')
    assert _python_index.python_index(repo, env) == "https://uv.example/simple"


def test_pip_command_section_and_explicit_file_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, env = _settings(tmp_path, monkeypatch)
    _pip_file(env, "[install]\nindex-url = https://user.example/simple\n")
    explicit = tmp_path / "pip.conf"
    explicit.write_text("[global]\nindex-url = https://explicit.example/simple\n")
    env["PIP_CONFIG_FILE"] = str(explicit)
    assert _python_index.python_index(repo, env) == "https://explicit.example/simple"
    explicit.write_text(
        "[global]\nindex-url = https://global.example/simple\n[install]\nindex-url = https://install.example/simple\n"
    )
    assert _python_index.python_index(repo, env) == "https://install.example/simple"
    env["PIP_CONFIG_FILE"] = _python_index.os.devnull
    assert _python_index.python_index(repo, env) == "https://pypi.org/simple"


def test_multiple_indexes_fail_without_disclosing_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, env = _settings(tmp_path, monkeypatch)
    env["UV_INDEX"] = "https://user:sentinel-secret@private.example/simple"
    with pytest.raises(ValueError) as caught:
        _python_index.python_index(repo, env)
    assert "sentinel-secret" not in str(caught.value)


def test_fresh_mirror_install_preserves_graph_hashes_markers_and_editable(
    python_mirror: PythonMirror,
) -> None:
    result = python_mirror.install("--no-dev")
    assert result.returncode == 0, result.stderr
    actual = python_mirror.inspect()
    assert actual["packages"] == {
        "mirror-probe": "1.0.0",
        "probe-runtime": "1.0.0",
        "probe-transitive": "1.0.0",
    }
    assert Path(str(actual["file"])).resolve() == (python_mirror.repo / "mirror_probe.py").resolve()
    assert actual["direct"] == {"url": python_mirror.repo.as_uri(), "dir_info": {"editable": True}}
    assert any("probe_runtime-1.0.0" in p for p in python_mirror.requests)
    assert not any("probe-dev" in p or "probe-platform" in p for p in python_mirror.requests)
    build = json.loads((python_mirror.repo / "build-proof.json").read_text())
    assert Path(build["prefix"]) != python_mirror.repo / ".venv"
    assert (python_mirror.repo / "uv.lock").read_bytes() == python_mirror.lock


def test_update_keeps_installed_dev_packages_and_official_mode_accepts_same_lock(
    python_mirror: PythonMirror,
) -> None:
    assert python_mirror.install().returncode == 0
    assert "probe-dev" in python_mirror.inspect()["packages"]
    python_mirror.requests.clear()
    result = python_mirror.install("--no-dev", "--reinstall-package", "mirror-probe")
    assert result.returncode == 0, result.stderr
    assert "probe-dev" in python_mirror.inspect()["packages"]
    assert not any("probe-dev" in p for p in python_mirror.requests)
    python_mirror.env.update(UV_DEFAULT_INDEX="https://pypi.org/simple", UV_OFFLINE="true")
    result = python_mirror.install("--no-dev")
    assert result.returncode == 0, result.stderr
    assert "probe-dev" in python_mirror.inspect()["packages"]
    assert (python_mirror.repo / "uv.lock").read_bytes() == python_mirror.lock


def test_stale_manifest_is_refused_before_environment_creation(python_mirror: PythonMirror) -> None:
    path = python_mirror.repo / "pyproject.toml"
    path.write_text(path.read_text().replace("probe-runtime==1.0.0", "probe-runtime==2.0.0"))
    result = python_mirror.install("--no-dev")
    assert result.returncode != 0
    assert not (python_mirror.repo / ".venv").exists()
    assert python_mirror.requests == []
    assert (python_mirror.repo / "uv.lock").read_bytes() == python_mirror.lock


def test_contaminated_lock_is_refused_before_export_or_install(python_mirror: PythonMirror) -> None:
    contaminated = python_mirror.lock.replace(
        b"https://pypi.org/simple", python_mirror.index.encode()
    )
    (python_mirror.repo / "uv.lock").write_bytes(contaminated)
    result = python_mirror.install("--no-dev")
    assert result.returncode != 0
    assert "Noncanonical" in result.stderr
    assert not (python_mirror.repo / ".venv").exists()
    assert python_mirror.requests == []
    assert (python_mirror.repo / "uv.lock").read_bytes() == contaminated


@pytest.mark.parametrize("unsafe_hash_setting", [False, True])
def test_mirror_cannot_change_locked_artifact_bytes(
    python_mirror: PythonMirror, unsafe_hash_setting: bool
) -> None:
    import zipfile

    with zipfile.ZipFile(python_mirror.wheel) as wheel:
        contents = {name: wheel.read(name) for name in wheel.namelist()}
    if unsafe_hash_setting:
        python_mirror.env["UV_NO_VERIFY_HASHES"] = "true"
    contents["probe_runtime.py"] += b"# tampered bytes\n"
    with zipfile.ZipFile(python_mirror.wheel, "w") as wheel:
        for name, data in contents.items():
            wheel.writestr(name, data)
    # A compromised mirror also changes its own advertised hash. The committed
    # lock's hash, not the mirror's metadata, must reject the modified wheel.
    index = python_mirror.wheel.parents[1] / "simple/probe-runtime/index.html"
    digest = hashlib.sha256(python_mirror.wheel.read_bytes()).hexdigest()
    index.write_text(
        f'<a href="../../packages/{python_mirror.wheel.name}#sha256={digest}">wheel</a>'
    )
    result = python_mirror.install("--no-dev")
    assert result.returncode != 0
    assert "hash" in result.stderr.lower()
    assert not (python_mirror.repo / "build-proof.json").exists()
    assert (python_mirror.repo / "uv.lock").read_bytes() == python_mirror.lock


def test_machine_pip_config_drives_real_mirror_download(python_mirror: PythonMirror) -> None:
    python_mirror.env.pop("UV_DEFAULT_INDEX")
    config = python_mirror.repo / "machine-pip.conf"
    config.write_text(f"[global]\nindex-url = {python_mirror.index}\n")
    python_mirror.env["PIP_CONFIG_FILE"] = str(config)
    original = config.read_bytes()
    result = python_mirror.install("--no-dev")
    assert result.returncode == 0, result.stderr
    assert any("probe_runtime-1.0.0" in p for p in python_mirror.requests)
    assert config.read_bytes() == original
    assert (python_mirror.repo / "uv.lock").read_bytes() == python_mirror.lock


@pytest.mark.parametrize(
    ("environment_key", "profile_key"),
    [("UV_INDEX_URL", "UV_DEFAULT_INDEX"), ("UV_DEFAULT_INDEX", "UV_INDEX_URL")],
)
def test_existing_mirror_profile_is_read_without_overriding_environment(
    python_mirror: PythonMirror,
    environment_key: str,
    profile_key: str,
) -> None:
    profile = python_mirror.repo / "mirror.env"
    profile.write_text(f"UV_DEFAULT_INDEX={python_mirror.index}\nnpm_config_registry=unused\n")
    python_mirror.env.pop("UV_DEFAULT_INDEX")
    result = python_mirror.install("--no-dev", "--mirror-env", str(profile))
    assert result.returncode == 0, result.stderr
    original = profile.read_bytes()
    python_mirror.env[environment_key] = python_mirror.index
    python_mirror.env["UV_HTTP_RETRIES"] = "0"
    profile.write_text(f"{profile_key}=http://127.0.0.1:1/unreachable\n")
    result = python_mirror.install(
        "--no-dev", "--mirror-env", str(profile), "--reinstall-package", "probe-runtime"
    )
    assert result.returncode == 0, result.stderr
    assert profile.read_text() == f"{profile_key}=http://127.0.0.1:1/unreachable\n"
    assert original.startswith(b"UV_DEFAULT_INDEX=http://127.0.0.1:")
    assert (python_mirror.repo / "uv.lock").read_bytes() == python_mirror.lock


def test_disabled_no_index_settings_do_not_override_a_configured_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, env = _settings(tmp_path, monkeypatch)
    _pip_file(env, "[global]\nindex-url = https://mirror.example/simple\nno-index = false\n")
    env.update(UV_NO_INDEX="false", PIP_NO_INDEX="0")
    assert _python_index.python_index(repo, env) == "https://mirror.example/simple"


def test_malformed_pip_file_error_does_not_disclose_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, env = _settings(tmp_path, monkeypatch)
    _pip_file(env, "index-url=https://user:sentinel-secret@private.example/simple\n")
    with pytest.raises(ValueError) as caught:
        _python_index.python_index(repo, env)
    assert "sentinel-secret" not in str(caught.value)


@pytest.mark.parametrize(
    ("environment_key", "profile_key"),
    [("UV_INDEX_URL", "UV_DEFAULT_INDEX"), ("UV_DEFAULT_INDEX", "UV_INDEX_URL")],
)
def test_explicit_official_index_overrides_saved_profile_alias(
    tmp_path: Path, environment_key: str, profile_key: str
) -> None:
    from cli.python_install import _configured_env

    profile = tmp_path / "mirror.env"
    profile.write_text(f"{profile_key}=https://mirror.example/simple\n")
    env = _configured_env({environment_key: "https://pypi.org/simple"}, profile)
    assert _python_index.python_index(tmp_path, env) == "https://pypi.org/simple"


def test_explicit_uv_config_is_not_reloaded_by_child_uv(python_mirror: PythonMirror) -> None:
    config = python_mirror.repo / "machine-uv.toml"
    config.write_text("this is deliberately invalid TOML\n")
    # An explicit index already won selection. The file must not override or
    # abort the child invocation: uv itself reads it even with --no-config.
    python_mirror.env["UV_CONFIG_FILE"] = str(config)
    result = python_mirror.install("--no-dev")
    assert result.returncode == 0, result.stderr
    assert any("probe_runtime-1.0.0" in p for p in python_mirror.requests)
    assert (python_mirror.repo / "uv.lock").read_bytes() == python_mirror.lock


def test_fresh_bootstrap_finds_checkout_code_with_safe_path(python_mirror: PythonMirror) -> None:
    import subprocess
    import sys

    from tests.cli._python_install_fixture import ROOT

    python_mirror.env["PYTHONSAFEPATH"] = "1"
    base_python = Path(sys.base_prefix) / (
        "python.exe" if sys.platform == "win32" else "bin/python3"
    )
    result = subprocess.run(  # noqa: S603 — managed Python and the trusted checkout entry point
        [
            "uv",
            "run",
            "--no-project",
            "--python",
            str(base_python),
            "python",
            str(ROOT / "cli/python_install.py"),
            "--repo",
            str(python_mirror.repo),
            "--python",
            sys.executable,
            "--no-dev",
        ],
        cwd=python_mirror.repo,
        env=python_mirror.env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert python_mirror.inspect()["direct"]["url"] == python_mirror.repo.as_uri()
    assert (python_mirror.repo / "uv.lock").read_bytes() == python_mirror.lock


def test_retained_updater_installs_after_reset_removes_target_helper(
    python_mirror: PythonMirror, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    # The orchestration imports its installer before switching source revisions.
    from cli.commands._update_uv_sync import run_uv_sync

    def git(*args: str) -> None:
        subprocess.run(  # noqa: S603 — disposable repository, no remote
            ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", *args],
            cwd=python_mirror.repo,
            capture_output=True,
            check=True,
            timeout=10,
        )

    git("init")
    git("add", ".")
    git("commit", "-m", "Historical project before installer helper")
    helper = python_mirror.repo / "cli/python_install.py"
    helper.parent.mkdir()
    helper.write_text('raise SystemExit("target helper must never be used")\n')
    git("add", ".")
    git("commit", "-m", "Later tree with helper")
    git("reset", "--hard", "HEAD~1")
    assert not helper.exists()
    for key, value in python_mirror.env.items():
        monkeypatch.setenv(key, value)
    result = run_uv_sync(python_mirror.repo)
    assert result.returncode == 0
    assert python_mirror.inspect()["direct"]["url"] == python_mirror.repo.as_uri()
    assert (python_mirror.repo / "uv.lock").read_bytes() == python_mirror.lock


def test_native_mirror_steps_share_one_deadline(
    python_mirror: PythonMirror, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli.commands import _update_uv_sync as native

    for key, value in python_mirror.env.items():
        monkeypatch.setenv(key, value)
    # Export, venv creation and dependencies consume one budget. The editable
    # build must not receive a fresh timeout when that budget has already elapsed.
    clock = iter([0.0, 1.0, 3.0, 8.0, 11.0])
    monkeypatch.setattr(native, "monotonic", lambda: next(clock))
    result = native.run_uv_sync(python_mirror.repo, timeout_s=10)
    assert result.returncode == 124
    assert not (python_mirror.repo / "build-proof.json").exists()
    assert any("probe_runtime-1.0.0" in path for path in python_mirror.requests)
    assert (python_mirror.repo / "uv.lock").read_bytes() == python_mirror.lock

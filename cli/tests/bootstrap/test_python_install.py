"""Real uv transport checks: immutable pins, host settings, and editable identity."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from cli import _python_index
from cli.tests.bootstrap._python_install_fixture import PythonMirror, build_python_mirror


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


def _assert_installs_precompile(calls: list[list[str]], *, expected: int) -> None:
    """`--no-config` hides `[tool.uv] compile-bytecode`, so every installing step carries the flag."""
    installing = [argv for argv in calls if argv[1] == "sync" or argv[1:3] == ["pip", "install"]]
    assert len(installing) == expected
    assert all("--compile-bytecode" in argv for argv in installing)
    assert not any("--compile-bytecode" in argv for argv in calls if argv not in installing)


@pytest.mark.parametrize("mirror_host", [True, False])
def test_environment_creation_names_the_checkout_python_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mirror_host: bool
) -> None:
    from cli import python_install
    from cli.tests.bootstrap._python_install_fixture import ROOT

    repo, env = _settings(tmp_path, monkeypatch)
    declared = (ROOT / ".python-version").read_text().strip()
    (repo / ".python-version").write_text(f"{declared}\n")
    (repo / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "probe"\nversion = "1.0.0"\nsource = { editable = "." }\n'
    )
    config = tmp_path / "xdg" / "uv" / "uv.toml"
    config.parent.mkdir(parents=True)
    if mirror_host:
        config.write_text('[[index]]\nurl = "https://mirror.example/simple"\ndefault = true\n')
    for key in list(os.environ):
        if key.startswith(("UV_", "PIP_")) or key == "VIRTUAL_ENV":
            monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("PIP_CONFIG_FILE", os.devnull)
    calls: list[list[str]] = []

    def record(
        argv: list[str], repo: Path, env: dict[str, str], *, discard_stdout: bool = False
    ) -> int:
        calls.append(argv)
        return 0

    assert python_install.install(repo, run=record) == 0
    # --no-config hides .python-version from uv; the creating step must carry it.
    [created] = [argv for argv in calls if argv[1] in {"sync", "venv"}]
    assert created[1] == ("venv" if mirror_host else "sync")
    assert "--no-config" in created
    assert created[created.index("--python") + 1] == declared
    # The offline export needs no interpreter; the pin must not block a later fetch.
    assert calls[0][1] == "export"
    assert "--python" not in calls[0]
    _assert_installs_precompile(calls, expected=1 + int(mirror_host))


def _declare_host_mirror_python(python_mirror: PythonMirror, tmp_path: Path) -> str:
    """Pin the running Python and name the mirror only in a host uv.toml.

    That host file selects the `uv venv --no-config` transport. Unpinned, uv
    takes the newest interpreter it finds.
    """
    declared = platform.python_version()
    (python_mirror.repo / ".python-version").write_text(f"{declared}\n")
    config = tmp_path / "xdg" / "uv" / "uv.toml"
    config.parent.mkdir(parents=True)
    config.write_text(f'[[index]]\nurl = "{python_mirror.index}"\ndefault = true\n')
    for key in ("UV_DEFAULT_INDEX", "UV_NO_CONFIG"):
        python_mirror.env.pop(key)
    base = Path(sys.base_prefix) if sys.platform == "win32" else Path(sys.base_prefix) / "bin"
    python_mirror.env.update(
        XDG_CONFIG_HOME=str(config.parents[1]),
        XDG_CONFIG_DIRS=str(tmp_path / "xdg-system"),
        PATH=f"{base}{os.pathsep}{python_mirror.env['PATH']}",
    )
    return declared


def _venv_python(python_mirror: PythonMirror) -> Path:
    bin_python = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    return python_mirror.repo / ".venv" / bin_python


def _reported(python: Path) -> str:
    return subprocess.run(  # noqa: S603 — the disposable virtualenv interpreter
        [str(python), "-c", "import platform; print(platform.python_version())"],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    ).stdout.strip()


def test_mirror_host_config_still_creates_the_declared_python(
    python_mirror: PythonMirror, tmp_path: Path
) -> None:
    # Discriminates on any host that also has a newer minor installed.
    declared = _declare_host_mirror_python(python_mirror, tmp_path)

    result = python_mirror.install("--no-dev", explicit_python=False)

    assert result.returncode == 0, result.stderr
    assert any("probe_runtime-1.0.0" in p for p in python_mirror.requests)
    assert _reported(_venv_python(python_mirror)) == declared


def test_mirror_recreates_an_environment_whose_interpreter_misses_the_pin(
    python_mirror: PythonMirror, tmp_path: Path
) -> None:
    declared = _declare_host_mirror_python(python_mirror, tmp_path)
    assert python_mirror.install("--no-dev", explicit_python=False).returncode == 0
    venv = python_mirror.repo / ".venv"
    marker = venv / "marker"
    marker.write_text("old environment")
    # Its lib/pythonX.Y directory and pyvenv.cfg still name the pin; only the
    # interpreter's own report reveals the drift.
    [site] = [*venv.glob("lib/python*/site-packages"), *venv.glob("Lib/site-packages")]
    (site / "sitecustomize.py").write_text(
        "import platform\nplatform.python_version = lambda: '3.11.0'\n"
    )
    interpreter = _venv_python(python_mirror)
    assert _reported(interpreter) == "3.11.0"

    result = python_mirror.install("--no-dev", explicit_python=False)

    assert result.returncode == 0, result.stderr
    assert f"Python 3.11.0 -> {declared}" in result.stderr
    assert not marker.exists()
    assert _reported(interpreter) == declared
    assert python_mirror.inspect()["packages"]["probe-runtime"] == "1.0.0"
    assert (python_mirror.repo / "uv.lock").read_bytes() == python_mirror.lock


def test_mirror_reuses_an_environment_on_the_declared_python(
    python_mirror: PythonMirror, tmp_path: Path
) -> None:
    _declare_host_mirror_python(python_mirror, tmp_path)
    assert python_mirror.install("--no-dev", explicit_python=False).returncode == 0
    marker = python_mirror.repo / ".venv" / "marker"
    marker.write_text("kept environment")
    (python_mirror.repo / ".venv" / "lib" / "python3.11").mkdir(parents=True)

    result = python_mirror.install("--no-dev", explicit_python=False)

    assert result.returncode == 0, result.stderr
    assert "Recreating" not in result.stderr
    assert marker.read_text() == "kept environment"


def test_venv_removal_refuses_a_link_out_of_the_checkout(tmp_path: Path) -> None:
    from cli.python_install import _remove_checkout_venv

    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep").write_text("not the checkout's")
    (repo / ".venv").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Refusing to remove"):
        _remove_checkout_venv(repo)
    assert (outside / "keep").read_text() == "not the checkout's"
    assert (repo / ".venv").is_symlink()


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


@pytest.mark.parametrize("official_index", [False, True])
def test_stale_lock_preserves_runtime_when_python_version_has_changed(
    python_mirror: PythonMirror, official_index: bool
) -> None:
    import re
    import subprocess
    import sys

    assert python_mirror.install("--no-dev").returncode == 0
    before = python_mirror.inspect()
    venv = python_mirror.repo / ".venv"
    interpreter = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    config = venv / "pyvenv.cfg"
    # Reproduce a managed interpreter alias advancing while this existing venv
    # still records its old patch version. Actual uv detects the mismatch.
    drifted, count = re.subn(
        r"(?m)^version_info = .+$", "version_info = 3.12.0", config.read_text()
    )
    assert count == 1
    config.write_text(drifted)
    manifest = python_mirror.repo / "pyproject.toml"
    manifest.write_text(
        manifest.read_text().replace("probe-runtime==1.0.0", "probe-runtime==2.0.0")
    )
    python_mirror.env["UV_OFFLINE"] = "true"
    if official_index:
        python_mirror.env["UV_DEFAULT_INDEX"] = "https://pypi.org/simple"
    python_mirror.requests.clear()

    result = python_mirror.install("--no-dev", "--verbose", "--python", str(interpreter))

    assert result.returncode != 0
    imported = subprocess.run(  # noqa: S603 — the disposable virtualenv interpreter
        [
            str(interpreter),
            "-c",
            "import probe_runtime, probe_transitive; assert probe_runtime.VALUE == probe_transitive.VALUE == 1",
        ],
        env=python_mirror.env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert imported.returncode == 0, imported.stderr
    assert config.read_text() == drifted
    assert python_mirror.inspect() == before
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

    from cli.tests.bootstrap._python_install_fixture import ROOT

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


def test_loading_installer_without_running_main_preserves_import_path(tmp_path: Path) -> None:
    from cli.tests.bootstrap._python_install_fixture import ROOT

    # Loading this stdlib-only entry as a library must expose install without
    # performing the standalone program's checkout bootstrap.
    result = subprocess.run(  # noqa: S603 — current interpreter and fixed library probe
        [
            sys.executable,
            "-I",
            "-c",
            "import runpy, sys; before = list(sys.path); "
            "module = runpy.run_path(sys.argv[1], run_name='installer_library'); "
            "assert callable(module['install']); assert sys.path == before",
            str(ROOT / "cli/python_install.py"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr

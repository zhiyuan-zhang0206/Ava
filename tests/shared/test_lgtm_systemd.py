"""Native user services retain home ownership, explicit binds and failure signals."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from cli.commands import _lgtm_native
from shared import lgtm_systemd
from shared.config import settings
from shared.lgtm_local import BACKENDS, backend_urls, binary_path


def _noop(*_args: object) -> None:
    return None


def _yes(*_args: object) -> bool:
    return True


def _control(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, 0, "", "")


def _loaded(*_args: object) -> dict[str, str]:
    return {"LoadState": "loaded", "ActiveState": "active", "MainPID": "123"}


def test_native_listener_ports_are_independent_of_external_query_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "isolated"
    native = home / "lgtm/native"
    monkeypatch.setattr(settings.observability, "lgtm_listen_host", "127.0.0.1")
    monkeypatch.setattr(settings.observability, "lgtm_grafana_listen_host", "127.0.0.1")
    for name, port in (
        ("loki", 53100),
        ("loki_grpc", 59095),
        ("prometheus", 59090),
        ("grafana", 53003),
    ):
        monkeypatch.setattr(settings.observability, f"lgtm_{name}_port", port)
    monkeypatch.setattr(settings.observability, "telemetry_loki_url", "https://query.example/loki")
    repo = Path(__file__).resolve().parents[2]
    _lgtm_native._render_configs(repo, native, home)
    loki = yaml.safe_load((native / "config/loki.yaml").read_text())
    assert loki["server"]["http_listen_port"] == 53100
    assert loki["server"]["grpc_listen_port"] == 59095
    assert "http_port = 53003" in (native / "config/grafana.ini").read_text()
    assert "GRAFANA_ROOT_URL:-http://localhost:53003}" in (native / "grafana/run.sh").read_text()
    argv, _ = _lgtm_native._service_invocation("prometheus", native, home)
    assert "--web.listen-address=127.0.0.1:59090" in argv
    assert backend_urls() == {
        "loki": "http://127.0.0.1:53100",
        "prometheus": "http://127.0.0.1:59090",
        "grafana": "http://127.0.0.1:53003",
    }


def test_systemd_names_include_full_home_identity(tmp_path: Path) -> None:
    first, second = tmp_path / "one/same home", tmp_path / "two/same home"
    assert lgtm_systemd.unit_name(first, "loki") != lgtm_systemd.unit_name(second, "loki")
    assert " " not in lgtm_systemd.unit_name(first, "loki")


def test_unit_arguments_do_not_expand_specifiers_or_variables(tmp_path: Path) -> None:
    home = tmp_path / "space $HOME %h"
    command = lgtm_systemd.Command(
        (str(binary_path(home, "loki")), f"-config.file={home}/loki.yaml"),
        {"AVA_HOME": str(home), "GOMEMLIMIT": "2GiB"},
    )
    unit = lgtm_systemd.render_unit(home, "loki", command)
    assert f"WorkingDirectory={str(home.resolve()).replace('%', '%%')}/lgtm/native\n" in unit
    assert (
        f"StandardOutput=append:{str(home.resolve()).replace('%', '%%')}/lgtm/native/logs/loki.log\n"
        in unit
    )
    assert 'ExecStart=:"' in unit
    assert "%%h" in unit and "$HOME" in unit
    assert "Restart=on-failure" in unit
    assert "KillMode=control-group" in unit
    assert "TimeoutStopSec=30" in unit
    with pytest.raises(ValueError, match="control characters"):
        lgtm_systemd._quote(f"{tmp_path}/path\nExecStart=unexpected")


def test_unit_registration_is_idempotent_and_leaves_other_homes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    calls: list[tuple[str, ...]] = []

    def control(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(lgtm_systemd, "_systemctl", control)
    home, foreign = tmp_path / "home", tmp_path / "other"
    foreign_path = lgtm_systemd.unit_path(foreign, "loki")
    foreign_path.parent.mkdir(parents=True)
    foreign_path.write_text("foreign unit")
    commands = {
        name: lgtm_systemd.Command((str(binary_path(home, name)),), {"AVA_HOME": str(home)})
        for name in BACKENDS
    }
    assert lgtm_systemd.register(home, commands)
    assert not lgtm_systemd.register(home, commands)
    assert calls == [("daemon-reload",)]
    assert foreign_path.read_text() == "foreign unit"


def test_foreign_loaded_fragment_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def foreign(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args,
            0,
            "LoadState=loaded\nActiveState=active\nMainPID=123\nFragmentPath=/foreign.service\n",
            "",
        )

    monkeypatch.setattr(lgtm_systemd, "_systemctl", foreign)
    with pytest.raises(RuntimeError, match="foreign"):
        lgtm_systemd.running_pid(tmp_path, "loki")


def test_owned_unit_requires_its_actual_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        lgtm_systemd,
        "_state",
        _loaded,
    )

    def foreign_binary(_: Path) -> Path:
        return Path("/another/home/bin/loki")

    def owned_binary(_: Path) -> Path:
        return Path(str(binary_path(tmp_path, "loki")) + " (deleted)")

    monkeypatch.setattr(Path, "readlink", foreign_binary)
    assert lgtm_systemd.running_pid(tmp_path, "loki") is None
    monkeypatch.setattr(Path, "readlink", owned_binary)
    assert lgtm_systemd.running_pid(tmp_path, "loki") == 123


def test_bad_loki_configuration_prevents_all_start_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject(_: Path) -> None:
        raise RuntimeError("invalid Loki config")

    monkeypatch.setattr(lgtm_systemd, "verify_loki", reject)

    def unexpected(*_args: str) -> subprocess.CompletedProcess[str]:
        pytest.fail("must not start any unit")

    monkeypatch.setattr(lgtm_systemd, "_systemctl", unexpected)
    with pytest.raises(RuntimeError, match="invalid Loki"):
        lgtm_systemd.start(tmp_path)


def test_systemctl_failure_is_not_reported_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lgtm_systemd.platform, "system", lambda: "Linux")

    def failed(*args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            [str(arg) for arg in args], 1, "", "user manager unavailable"
        )

    monkeypatch.setattr(lgtm_systemd.subprocess, "run", failed)
    with pytest.raises(RuntimeError, match="user manager unavailable"):
        lgtm_systemd._systemctl("daemon-reload")


def test_stop_removes_all_owned_units_without_touching_foreign_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    home = tmp_path / "home"
    foreign = lgtm_systemd.unit_path(tmp_path / "foreign", "grafana")
    foreign.parent.mkdir(parents=True)
    foreign.write_text("foreign")
    for name in BACKENDS:
        lgtm_systemd.unit_path(home, name).write_text("owned")
    calls: list[tuple[str, ...]] = []

    def control(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(lgtm_systemd, "_systemctl", control)
    monkeypatch.setattr(lgtm_systemd, "_state", _loaded)
    monkeypatch.setattr(lgtm_systemd, "running_pid", _noop)
    lgtm_systemd.stop(home)
    assert calls == [
        *(("disable", "--now", lgtm_systemd.unit_name(home, name)) for name in reversed(BACKENDS)),
        ("daemon-reload",),
    ]
    assert all(not lgtm_systemd.unit_path(home, name).exists() for name in BACKENDS)
    assert foreign.read_text() == "foreign"


def test_foreign_http_listener_cannot_satisfy_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    path = lgtm_systemd.unit_path(tmp_path, "loki")
    path.parent.mkdir(parents=True)
    path.touch()
    monkeypatch.setattr(lgtm_systemd, "verify_loki", _noop)
    monkeypatch.setattr(lgtm_systemd, "running_pid", _noop)
    monkeypatch.setattr(lgtm_systemd, "_answers", _yes)
    monkeypatch.setattr(lgtm_systemd.time, "sleep", _noop)
    monkeypatch.setattr(
        lgtm_systemd,
        "_systemctl",
        _control,
    )
    with pytest.raises(RuntimeError, match="loki did not become reachable"):
        lgtm_systemd.start(tmp_path)


def test_matching_versions_from_another_platform_are_downloaded_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    native = home / "lgtm/native"
    native.mkdir(parents=True)
    monkeypatch.setattr(_lgtm_native, "platform_tag", lambda: "linux_amd64")
    repo = Path(__file__).resolve().parents[2]
    assets = _lgtm_native._load_versions(repo)
    for name, asset in assets.items():
        (native / f"version-{name}").write_text(asset["version"])
        (native / f"platform-{name}").write_text("darwin_arm64")
    downloads: list[str] = []

    def download(name: str, _version: str, asset: dict[str, str], _native: Path) -> None:
        assert "linux" in asset["url"]
        if name != "grafana":
            assert "linux-amd64" in asset["member"]
        downloads.append(name)

    monkeypatch.setattr(_lgtm_native, "_download_and_verify", download)
    monkeypatch.setattr(lgtm_systemd, "register", _yes)
    monkeypatch.setattr(lgtm_systemd, "restart_running", _noop)
    _lgtm_native.ensure_lgtm_native(repo, home)
    assert downloads == list(BACKENDS)


def test_rendered_paths_pass_the_real_systemd_parser(tmp_path: Path) -> None:
    """Keep path directives distinct from ExecStart's argument grammar."""
    import shutil

    from shared.process_env import inherited_process_env

    analyze = shutil.which("systemd-analyze")
    if analyze is None:
        pytest.skip("systemd-analyze is available on Linux hosts")
    home = tmp_path / 'space %h "quote"'
    native = home / "lgtm/native"
    (native / "logs").mkdir(parents=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    executable = shutil.which("true")
    assert executable is not None
    unit = tmp_path / "ava-native-parser-test.service"
    unit.write_text(lgtm_systemd.render_unit(home, "loki", lgtm_systemd.Command((executable,), {})))
    parsed = subprocess.run(  # noqa: S603 — installed parser and a generated private unit
        [analyze, "--user", "verify", str(unit)],
        env=inherited_process_env({"XDG_RUNTIME_DIR": str(runtime)}),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert parsed.returncode == 0, parsed.stderr
    assert not any(word in parsed.stderr for word in ("Failed to parse", "Unknown key", "Invalid"))

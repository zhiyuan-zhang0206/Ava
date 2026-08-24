"""Portable lifecycle tests for the native LGTM start and stop scripts."""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
START = ROOT / "deploy/lgtm/start.sh"
STOP = ROOT / "deploy/lgtm/stop.sh"


def _executable(path: Path, source: str) -> None:
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    path.chmod(0o755)


def _toolset(tmp_path: Path) -> tuple[dict[str, str], Path]:
    tools = tmp_path / "tools"
    tools.mkdir()
    log = tmp_path / "tool.log"
    up = tmp_path / "up"
    up.mkdir()
    loaded = tmp_path / "loaded"
    loaded.mkdir()
    _executable(
        tools / "launchctl",
        """\
        #!/usr/bin/env bash
        printf '%s\\n' "$*" >> "$FAKE_LOG"
        if [[ "$1" == "kickstart" ]]; then
          case "$2" in
            *loki*) touch "$FAKE_UP/loki" ; touch "$FAKE_LOADED/loki" ;;
            *prometheus*) touch "$FAKE_UP/prometheus" ; touch "$FAKE_LOADED/prometheus" ;;
            *grafana*) touch "$FAKE_UP/grafana" ; touch "$FAKE_LOADED/grafana" ;;
          esac
        fi
        if [[ "$1" == "print" ]]; then
          case "$2" in
            *grafana-native*) [[ -e "$FAKE_LOADED/grafana-native" ]] && exit 0 || exit 1 ;;
            *loki*) [[ -e "$FAKE_LOADED/loki" ]] && exit 0 || exit 1 ;;
            *prometheus*) [[ -e "$FAKE_LOADED/prometheus" ]] && exit 0 || exit 1 ;;
            *grafana*) [[ -e "$FAKE_LOADED/grafana" ]] && exit 0 || exit 1 ;;
          esac
          exit 1
        fi
        if [[ "$1" == "bootout" && "$2" == *grafana-native* ]]; then
          rm -f "$FAKE_LOADED/grafana-native"
          exit 0
        fi
        [[ "$1" == "bootout" ]] && exit 1
        exit 0
        """,
    )
    _executable(tools / "sleep", "#!/usr/bin/env bash\n")
    _executable(
        tools / "curl",
        """\
        #!/usr/bin/env bash
        url="${!#}"
        if [[ "$url" == *"/api/v1/provisioning/alert-rules" ]]; then
          count="${FAKE_RULE_COUNT:-19}"
          printf '['
          for index in $(seq 1 "$count"); do
            [[ "$index" != 1 ]] && printf ','
            printf '{"uid":"rule-%s","data":[{"model":{"datasource":{"uid":"loki"}}}]}' "$index"
          done
          printf ']'
          exit 0
        fi
        case "$url" in
          *:3100/*) name=loki ;;
          *:9090/*) name=prometheus ;;
          *:3003/*) name=grafana ;;
          *) printf '200' ; exit 0 ;;
        esac
        if [[ "$name" == "grafana" && -n "${FAKE_GRAFANA_UNREACHABLE:-}" ]]; then
          printf '000'
          exit 0
        fi
        [[ -e "$FAKE_UP/$name" ]] && printf '200' || printf '000'
        """,
    )
    home = tmp_path / "home"
    native = home / "lgtm/native/bin"
    native.mkdir(parents=True)
    for name in ("loki", "prometheus"):
        binary = native / name
        binary.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        binary.chmod(0o755)
    (home / "lgtm/native/grafana").mkdir(parents=True)
    _executable(
        home / "lgtm/native/grafana/run.sh",
        "#!/usr/bin/env bash\n",
    )
    agents_dir = home / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True)
    for name in ("loki", "prometheus", "grafana"):
        (agents_dir / f"com.ava.{name}.home-slug.plist").touch()
    env = {
        **os.environ,
        "HOME": str(home),
        "AVA_HOME": str(home),
        "FAKE_LOG": str(log),
        "FAKE_UP": str(up),
        "FAKE_LOADED": str(loaded),
        "PATH": f"{tools}:{os.environ['PATH']}",
    }
    return env, log


def _run(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed repository script in an isolated test toolset
        ["bash", str(script)], env=env, capture_output=True, text=True, check=False
    )


def test_start_and_stop_are_idempotent_without_real_services(tmp_path: Path) -> None:
    env, log = _toolset(tmp_path)

    first = _run(START, env)
    assert first.returncode == 0, first.stderr + first.stdout
    first_lines = log.read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("bootstrap ") for line in first_lines) == 3
    assert sum(line.startswith("kickstart ") for line in first_lines) == 3

    log.write_text("", encoding="utf-8")
    second = _run(START, env)
    assert second.returncode == 0, second.stderr + second.stdout
    second_lines = log.read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith("bootstrap ") for line in second_lines)
    assert not any(line.startswith("kickstart ") for line in second_lines)

    # Clear the up markers so the stop assertions see exactly its four bootout attempts.
    Path(env["FAKE_UP"]).mkdir(exist_ok=True)
    for child in Path(env["FAKE_UP"]).iterdir():
        child.unlink()
    log.write_text("", encoding="utf-8")
    assert _run(STOP, env).returncode == 0
    assert _run(STOP, env).returncode == 0
    stopped = log.read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("bootout ") for line in stopped) == 4
    assert stopped.count("compose down") == 0


def test_start_fails_when_a_native_binary_is_missing(tmp_path: Path) -> None:
    env, _log = _toolset(tmp_path)
    (Path(env["AVA_HOME"]) / "lgtm/native/bin/loki").unlink()

    result = _run(START, env)

    assert result.returncode == 1
    assert "ERROR:" in result.stdout
    assert "run converge / `ava lgtm on`" in result.stdout


def test_start_counts_provisioned_rules_as_json_documents(tmp_path: Path) -> None:
    env, _log = _toolset(tmp_path)
    env["FAKE_RULE_COUNT"] = "17"
    password_file = Path(env["AVA_HOME"]) / "lgtm/native/grafana/admin_password"
    password_file.write_text("test-password\n", encoding="utf-8")

    result = _run(START, env)

    assert result.returncode == 0, result.stderr + result.stdout
    assert "Grafana provisioned 17 alert rules; expected at least 18" in result.stdout


def test_start_retires_legacy_grafana_only_after_the_replacement_is_reachable(
    tmp_path: Path,
) -> None:
    env, log = _toolset(tmp_path)
    home = Path(env["AVA_HOME"])
    legacy_plist = home / "Library/LaunchAgents/com.ava.grafana-native.plist"
    legacy_plist.touch()
    (Path(env["FAKE_LOADED"]) / "grafana-native").touch()

    result = _run(START, env)

    assert result.returncode == 0, result.stderr + result.stdout
    assert f"bootout gui/{os.getuid()}/com.ava.grafana-native" in log.read_text(encoding="utf-8")
    assert not legacy_plist.exists()


def test_start_preserves_legacy_grafana_when_replacement_never_becomes_reachable(
    tmp_path: Path,
) -> None:
    env, log = _toolset(tmp_path)
    env["FAKE_GRAFANA_UNREACHABLE"] = "1"
    home = Path(env["AVA_HOME"])
    legacy_plist = home / "Library/LaunchAgents/com.ava.grafana-native.plist"
    legacy_plist.touch()
    (Path(env["FAKE_LOADED"]) / "grafana-native").touch()

    result = _run(START, env)

    assert result.returncode == 1
    assert "com.ava.grafana-native" not in log.read_text(encoding="utf-8")
    assert legacy_plist.exists()

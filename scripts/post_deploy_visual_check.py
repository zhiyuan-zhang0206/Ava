#!/usr/bin/env python3
"""Run the five-surface post-deploy visual gate in pinned Playwright Docker."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PLAYWRIGHT_IMAGE = "mcr.microsoft.com/playwright/python:v1.59.0-noble"
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.post_deploy_visual_policy import (  # noqa: E402
    extract_gateway_sha,
    extract_gateway_started_at,
    validate_wave_id,
)
from shared.process_env import inherited_process_env  # noqa: E402

DEFAULT_OUTPUT_ROOT = Path.home() / "post-deploy-visual"
IGNORE_REGISTRY = Path(__file__).with_name("post_deploy_visual_known_ignores.json")
CONTAINER_TIMEOUT_SECONDS = 28 * 60


def _health_payload(base_url: str) -> dict[str, object]:
    if urllib.parse.urlsplit(base_url).scheme not in {"http", "https"}:
        raise ValueError("--base-url must use http or https")
    request = urllib.request.Request(  # noqa: S310 - only HTTP(S) survives validation below
        f"{base_url.rstrip('/')}/api/health", method="GET"
    )
    try:
        response = urllib.request.urlopen(request, timeout=10)  # noqa: S310 - operator URL
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise TypeError("gateway health response must be a JSON object")
    return payload


def _git_output(arguments: list[str]) -> str:
    result = subprocess.run(  # noqa: S603 - fixed executable with internal git arguments
        ["git", *arguments],
        cwd=REPO_ROOT,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _wave_changes(
    previous_sha: str | None, wave_sha: str
) -> tuple[list[str], list[dict[str, str]]]:
    if previous_sha is None or previous_sha == wave_sha:
        return [], []
    paths = _git_output(["diff", "--name-only", f"{previous_sha}..{wave_sha}", "--", "ui/web"])
    log = _git_output(["log", "--format=%H%x09%s", f"{previous_sha}..{wave_sha}", "--", "ui/web"])
    commits = []
    for line in log.splitlines():
        sha, subject = line.split("\t", 1)
        match = re.search(r"\(#(\d+)\)$", subject)
        commits.append({"sha": sha, "subject": subject, "pr": match.group(1) if match else ""})
    return paths.splitlines(), commits


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _cookie_mount(cookie_file: Path) -> tuple[str, str]:
    if not cookie_file.is_file():
        raise FileNotFoundError(f"cookie file does not exist: {cookie_file}")
    mode = stat.S_IMODE(cookie_file.stat().st_mode)
    if mode != 0o600:
        raise PermissionError(f"{cookie_file} must have mode 0600, found {mode:04o}")
    return f"{cookie_file}:/run/ava-visual-cookie:ro", "/run/ava-visual-cookie"


def _container_command(
    args: argparse.Namespace,
    *,
    output_root: Path,
    input_file: Path,
    cookie_file: Path | None,
    demo_internal: bool = False,
) -> list[str]:
    container_cookie: str | None = None
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        _container_name(),
        "--platform",
        "linux/amd64",
        "-v",
        f"{REPO_ROOT}:/workspace:ro",
        "-v",
        f"{output_root}:/artifacts",
        "-w",
        "/workspace",
    ]
    if cookie_file is not None:
        mount, container_cookie = _cookie_mount(cookie_file)
        command.extend(["-v", mount])
    command.extend(
        [
            PLAYWRIGHT_IMAGE,
            "bash",
            "-lc",
            (
                "python -m pip install --quiet --disable-pip-version-check "
                "--root-user-action=ignore playwright==1.59.0 && "
                'exec python -m scripts.post_deploy_visual_check "$@"'
            ),
            "ava-visual",
            "--inside-container",
            "--base-url",
            args.base_url,
            "--wave-sha",
            args.wave_sha,
            "--output-root",
            "/artifacts",
            "--input-file",
            f"/artifacts/{input_file.relative_to(output_root)}",
        ]
    )
    if demo_internal:
        command.append("--demo-internal")
    elif container_cookie is not None:
        command.extend(["--cookie-file", container_cookie])
    return command


def _container_name() -> str:
    return f"ava-post-deploy-visual-{os.getpid()}"


def _validate_demo_target(value: str) -> None:
    parsed = urllib.parse.urlsplit(value)
    if parsed.hostname != "host.docker.internal" or parsed.port not in range(3001, 3101):
        raise ValueError("demo container target must be host.docker.internal on port 3001..3100")


def _expected_capture_names() -> set[str]:
    names = set()
    for viewport in ("desktop", "narrow"):
        surfaces = ["login-card", "control-header", "control-nav", "home-header", "home-composer"]
        if viewport == "desktop":
            surfaces.append("home-sidebar")
        for theme in ("light", "dark"):
            for surface in surfaces:
                names.update(
                    f"{surface}-{viewport}-{theme}-current-{frame}.png" for frame in (1, 2)
                )
    return names


def _run_container(command: list[str]) -> int:
    try:
        return subprocess.run(  # noqa: S603 - argv is assembled without a shell
            command,
            check=False,
            timeout=CONTAINER_TIMEOUT_SECONDS,
        ).returncode
    except subprocess.TimeoutExpired:
        subprocess.run(  # noqa: S603 - exact name belongs to this process
            ["docker", "rm", "--force", _container_name()],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(
            json.dumps(
                {"result": "error", "detail": "visual container exceeded its 28-minute budget"}
            ),
            file=sys.stderr,
        )
        return 1


def _accept_wave(args: argparse.Namespace) -> int:
    if not args.accepted_by:
        raise ValueError("--accept-wave requires --accepted-by")
    output_root = args.output_root.resolve()
    wave_id = validate_wave_id(args.accept_wave)
    wave_dir = output_root / wave_id
    captures = wave_dir / "captures"
    if not (wave_dir / "probes.json").is_file() or not captures.is_dir():
        raise FileNotFoundError(f"wave artifacts are incomplete: {wave_dir}")
    probes = _read_json(wave_dir / "probes.json", {})
    failures = probes["structural_failures"]
    matrix = probes["matrix"]
    if not isinstance(failures, list) or not isinstance(matrix, list):
        raise TypeError("wave probes must contain structural failure and matrix lists")
    if failures:
        raise RuntimeError("a wave with structural failures cannot become the golden")
    if len(matrix) != 20:
        raise RuntimeError(
            f"wave matrix is incomplete: expected 20 combinations, found {len(matrix)}"
        )
    state = _read_json(output_root / "state.json", {})
    if state.get("golden_sha") == wave_id:
        raise ValueError(f"wave is already the golden: {wave_id}")
    golden = output_root / "golden" / wave_id / "captures"
    golden.mkdir(parents=True, exist_ok=True)
    present = {path.name for path in captures.glob("*-current-[12].png")}
    missing = sorted(_expected_capture_names() - present)
    if missing:
        raise RuntimeError(f"wave is missing {len(missing)} required captures")
    accepted = []
    for source in sorted(captures.glob("*-current-[12].png")):
        target = golden / source.name.replace("-current-", "-golden-")
        shutil.copy2(source, target)
        accepted.append(target.name)
    if not accepted:
        raise RuntimeError("wave contains no pixel-comparison captures")
    record = {
        "accepted_at": datetime.now(UTC).isoformat(),
        "accepted_by": args.accepted_by,
        "wave_sha": wave_id,
        "captures": accepted,
    }
    audit = output_root / "acceptance-audit.jsonl"
    with audit.open("a") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
    audit.chmod(0o600)
    state["golden_sha"] = wave_id
    state["unexpected_wave_counts"] = {}
    _write_json(output_root / "state.json", state)
    print(json.dumps({"result": "accepted", **record}, sort_keys=True))
    return 0


def _find_preview_port() -> int:
    for port in range(3001, 3101):
        with socket.socket() as candidate:
            if candidate.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("no free preview port in 3001..3100")


def _wait_for_preview(url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("local preview exited before becoming ready")
        try:
            with urllib.request.urlopen(url, timeout=2):  # noqa: S310 - loopback only
                return
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)
    raise TimeoutError("local preview did not become ready")


@contextlib.contextmanager
def _isolated_frontend_preview() -> Generator[Path]:
    frontend = REPO_ROOT / "ui" / "web"
    if not (frontend / "node_modules" / "next").exists():
        raise FileNotFoundError("demo requires ui/web/node_modules; run npm ci first")
    builds = frontend / ".builds"
    builds.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="visual-demo-", dir=builds) as temporary:
        preview = Path(temporary)
        shutil.copytree(
            frontend,
            preview,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                "node_modules", ".next", ".builds", "coverage", "*.log", "junit.xml"
            ),
        )
        (preview / "node_modules").symlink_to(Path("../../node_modules"))
        yield preview


def _run_demo_preview(args: argparse.Namespace, preview: Path) -> int:
    port = _find_preview_port()
    host_url = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(  # noqa: S603 - fixed local preview command
        [
            "npm",
            "run",
            "dev",
            "--",
            "--hostname",
            "0.0.0.0",  # noqa: S104 - Docker Desktop must reach this loopback-only demo host
            "--port",
            str(port),
        ],
        cwd=preview,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        text=True,
        env=inherited_process_env({"AVA_DEV_ORIGINS": "host.docker.internal"}),
        start_new_session=True,
    )
    try:
        _wait_for_preview(host_url, process)
        args.base_url = f"http://host.docker.internal:{port}"
        args.wave_sha = validate_wave_id(f"demo-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}")
        wave_dir = args.output_root.resolve() / args.wave_sha
        input_file = wave_dir / "_input.json"
        _write_json(input_file, {"deployment_wave": False, "changed_paths": [], "commits": []})
        return _run_container(
            _container_command(
                args,
                output_root=args.output_root.resolve(),
                input_file=input_file,
                cookie_file=None,
                demo_internal=True,
            )
        )
    finally:
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def _run_demo(args: argparse.Namespace) -> int:
    with _isolated_frontend_preview() as preview:
        return _run_demo_preview(args, preview)


def _run_host(args: argparse.Namespace) -> int:
    if args.accept_wave:
        return _accept_wave(args)
    if args.demo_defect:
        return _run_demo(args)
    if not args.base_url:
        raise ValueError("--base-url is required")
    cookie_value = inherited_process_env().get("AVA_VISUAL_GATE_COOKIE_FILE")
    if not cookie_value:
        raise ValueError("AVA_VISUAL_GATE_COOKIE_FILE is required")
    cookie_file = Path(cookie_value).resolve()
    health_base = args.health_url or args.base_url.rstrip("/")
    health = _health_payload(health_base)
    started_at = extract_gateway_started_at(health)
    serving_sha = extract_gateway_sha(health)
    output_root = args.output_root.resolve()
    state = _read_json(output_root / "state.json", {})
    previous_started = state.get("gateway_started_at")
    deployment_wave = not args.check or previous_started != started_at
    if args.wave_sha is not None and args.wave_sha != serving_sha:
        raise ValueError("--wave-sha does not match the commit reported by the serving gateway")
    args.wave_sha = validate_wave_id(serving_sha)
    changed_paths, commits = _wave_changes(state.get("golden_sha"), args.wave_sha)
    input_file = output_root / args.wave_sha / "_input.json"
    _write_json(
        input_file,
        {
            "deployment_wave": deployment_wave,
            "gateway_started_at": started_at,
            "changed_paths": changed_paths,
            "commits": commits,
        },
    )
    return _run_container(
        _container_command(
            args,
            output_root=output_root,
            input_file=input_file,
            cookie_file=cookie_file,
        )
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    parser.add_argument(
        "--health-url",
        help=(
            "gateway origin for the host-side health probe (the script appends "
            "/api/health); defaults to --base-url. Required when --base-url is "
            "a gate that serves the SPA wall for /api instead of proxying to "
            "the gateway."
        ),
    )
    parser.add_argument("--wave-sha")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--accept-wave")
    parser.add_argument("--accepted-by")
    parser.add_argument("--demo-defect", action="store_true")
    parser.add_argument("--inside-container", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--demo-internal", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--input-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--cookie-file", type=Path, help=argparse.SUPPRESS)
    return parser


def _run_browser(args: argparse.Namespace) -> int:
    if not args.base_url or not args.wave_sha or args.input_file is None:
        raise ValueError("container mode requires --base-url, --wave-sha, and --input-file")
    args.wave_sha = validate_wave_id(args.wave_sha)
    if args.demo_internal:
        _validate_demo_target(args.base_url)
    from scripts.post_deploy_visual_runner import run_browser_gate

    return run_browser_gate(
        base_url=args.base_url,
        wave_sha=args.wave_sha,
        output_root=args.output_root,
        input_file=args.input_file,
        cookie_file=args.cookie_file,
        demo=args.demo_internal,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.inside_container:
            return _run_browser(args)
        return _run_host(args)
    except (
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        subprocess.CalledProcessError,
    ) as exc:
        print(json.dumps({"result": "error", "detail": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

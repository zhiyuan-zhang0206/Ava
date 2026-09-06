"""Disposable wheels/index for actual uv tests, with no public registry traffic."""

from __future__ import annotations

import functools
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

import pytest

ROOT = Path(__file__).resolve().parents[2]


class InstalledState(TypedDict):
    file: str
    packages: dict[str, str]
    direct: dict[str, object]


@dataclass
class PythonMirror:
    repo: Path
    index: str
    env: dict[str, str]
    requests: list[str]
    wheel: Path
    lock: bytes

    def install(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 — fixed commands against disposable test inputs
            [
                sys.executable,
                "-m",
                "cli.python_install",
                "--repo",
                str(self.repo),
                "--python",
                sys.executable,
                *args,
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

    def inspect(self) -> InstalledState:
        python = (
            self.repo
            / ".venv"
            / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        )
        result = subprocess.run(  # noqa: S603 — fixed commands against disposable test inputs
            [
                str(python),
                "-c",
                "import json,importlib.metadata as m,mirror_probe; "
                "print(json.dumps({'file':mirror_probe.__file__,"
                "'packages':{d.metadata['Name']:d.version for d in m.distributions()},"
                "'direct':json.loads(m.distribution('mirror-probe').read_text('direct_url.json'))}))",
            ],
            cwd=self.repo,
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
            timeout=20,
        )
        return json.loads(result.stdout)


def _wheel(root: Path, name: str, requires: str = "") -> Path:
    normalized = name.replace("-", "_")
    filename = f"{normalized}-1.0.0-py3-none-any.whl"
    path = root / "packages" / filename
    path.parent.mkdir(exist_ok=True)
    info = f"{normalized}-1.0.0.dist-info"
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr(f"{normalized}.py", "VALUE = 1\n")
        wheel.writestr(
            f"{info}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0.0\n{requires}"
        )
        wheel.writestr(
            f"{info}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        )
        wheel.writestr(f"{info}/RECORD", "")
    index = root / "simple" / name
    index.mkdir(parents=True)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (index / "index.html").write_text(
        f'<a href="../../packages/{filename}#sha256={digest}">{filename}</a>'
    )
    return path


def build_python_mirror(tmp_path: Path) -> Iterator[PythonMirror]:
    if shutil.which("uv") is None:
        pytest.skip("real uv binary required")
    site = tmp_path / "index"
    site.mkdir()
    runtime = _wheel(site, "probe-runtime", "Requires-Dist: probe-transitive==1.0.0\n")
    for name in ["probe-transitive", "probe-dev", "probe-platform"]:
        _wheel(site, name)
    requests: list[str] = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            requests.append(self.path)
            super().do_GET()

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(site))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    mirror = f"http://127.0.0.1:{server.server_port}"
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("""[project]
name = "mirror-probe"
version = "1.0.0"
requires-python = ">=3.12"
dependencies = ["probe-runtime==1.0.0", "probe-platform==1.0.0; sys_platform == 'not-a-real-platform'"]
[dependency-groups]
dev = ["probe-dev==1.0.0"]
[build-system]
requires = []
build-backend = "probe_backend"
backend-path = ["."]
""")
    (repo / "mirror_probe.py").write_text("VALUE = 1\n")
    (repo / "probe_backend.py").write_text("""from pathlib import Path
from typing import TypedDict
from zipfile import ZipFile
import json, sys

def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    name = 'mirror_probe-1.0.0-py3-none-any.whl'
    with ZipFile(Path(wheel_directory)/name, 'w') as z:
        z.writestr('mirror_probe.pth', str(Path(__file__).parent))
        z.writestr('mirror_probe-1.0.0.dist-info/METADATA', 'Metadata-Version: 2.1\\nName: mirror-probe\\nVersion: 1.0.0\\nRequires-Dist: probe-runtime==1.0.0\\n')
        z.writestr('mirror_probe-1.0.0.dist-info/WHEEL', 'Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n')
        z.writestr('mirror_probe-1.0.0.dist-info/RECORD', '')
    Path(__file__).with_name('build-proof.json').write_text(json.dumps({'prefix':sys.prefix}))
    return name

def build_wheel(*args, **kwargs): return build_editable(*args, **kwargs)
""")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("UV_", "PIP_")) and k != "VIRTUAL_ENV"
    }
    env.update(
        UV_CACHE_DIR=str(tmp_path / "cache"),
        UV_PYTHON_DOWNLOADS="never",
        UV_NO_CONFIG="true",
        PIP_CONFIG_FILE=os.devnull,
    )
    try:
        subprocess.run(  # noqa: S603 — fixed commands against disposable test inputs
            [
                "uv",
                "lock",
                "--no-config",
                "--python",
                sys.executable,
                "--default-index",
                mirror + "/simple",
            ],
            cwd=repo,
            env=env,
            capture_output=True,
            check=True,
            timeout=30,
        )
        # Synthetic test artifacts only: project pins/metadata/hashes are uv-generated.
        # Give the fixture canonical origins; only the download stage knows localhost.
        lock = (
            (repo / "uv.lock")
            .read_bytes()
            .replace((mirror + "/simple").encode(), b"https://pypi.org/simple")
            .replace((mirror + "/packages/").encode(), b"https://files.pythonhosted.org/packages/")
        )
        (repo / "uv.lock").write_bytes(lock)
        shutil.rmtree(tmp_path / "cache")
        requests.clear()
        env["UV_DEFAULT_INDEX"] = mirror + "/simple"
        yield PythonMirror(repo, mirror + "/simple", env, requests, runtime, lock)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

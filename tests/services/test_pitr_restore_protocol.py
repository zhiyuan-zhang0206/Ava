"""The restricted producer exposes one complete result before controller acceptance."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from services.pitr import worker_process
from shared.exec_process_domain import ExecProcessDomain


async def test_native_restore_result_is_atomic_before_receiver_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker, release = tmp_path / "publishing", tmp_path / "release"
    script = tmp_path / "producer.py"
    script.write_text(
        "import pathlib,sys,time\n"
        f"sys.path.insert(0, {str(Path.cwd())!r})\n"
        "from services.pitr.worker_process import publish_result\n"
        "result,marker,release=map(pathlib.Path,sys.argv[1:])\n"
        "replace,write=pathlib.Path.replace,pathlib.Path.write_text\n"
        "def pause():\n"
        " marker.write_text('publishing')\n"
        " while not release.exists(): time.sleep(.005)\n"
        "def staged(path,target):\n"
        " if target==result: pause()\n"
        " return replace(path,target)\n"
        "def direct(path,data,*args,**kwargs):\n"
        " if path==result:\n"
        "  with path.open('w') as output:\n"
        "   pause(); return output.write(data)\n"
        " return write(path,data,*args,**kwargs)\n"
        "pathlib.Path.replace=staged; pathlib.Path.write_text=direct\n"
        "publish_result(result,dict(chain_id='chain',candidate_sha256='candidate',"
        "pending_sha256='pending'))\n"
    )
    factory = ExecProcessDomain.launch_posix
    children: list[tuple[subprocess.Popen[bytes], ExecProcessDomain, Path]] = []

    def launch(argv: list[str], **kwargs: Any):
        result = Path(argv[-1])
        process, domain = factory(
            [sys.executable, "-I", "-B", str(script), str(result), str(marker), str(release)],
            **kwargs,
        )
        children.append((process, domain, result))
        return process, domain

    monkeypatch.setattr(ExecProcessDomain, "launch_posix", launch)
    task = asyncio.create_task(
        worker_process.run_operation(
            "services.pitr.restore_worker",
            {},
            control_root=tmp_path / "controls",
            env={},
        )
    )
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert marker.is_file()
        result = children[0][2]
        assert not result.exists(), "partial final result exposed before publication"
        (staged,) = result.parent.glob(f".{result.name}.*.tmp")
        assert json.loads(staged.read_text())["chain_id"] == "chain"
        assert staged.stat().st_mode & 0o777 == 0o600
        await asyncio.sleep(0.3)  # the controller must still be awaiting publication
        assert not task.done()
        release.touch()
        completed = await asyncio.wait_for(task, 5)
        assert completed.result["chain_id"] == "chain"
        assert children[0][0].returncode == 0
        completed.retire()
        assert not result.parent.exists()
    finally:
        release.touch()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        _close([(process, domain) for process, domain, _result in children])


def _close(children: list[tuple[subprocess.Popen[bytes], ExecProcessDomain]]) -> None:
    for process, domain in children:
        if process.returncode is None:
            domain.close_confirmed(time.monotonic() + 5)
            process.wait(timeout=5)

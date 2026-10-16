from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from base.config.tests.configuration import snapshot_process_config
from base.deploy.git.memory_repo import init_local_repo, run_git
from base.deploy.git.tests.memory_checkouts import commit_note
from base.deploy.git.tests.memory_checkouts import memory_checkout as memory_checkout
from gateway.http.config import gateway_config_from_boot
from tests.fixtures.gateway_config import gateway_test_client


def test_refresh_updates_the_checkout_read_by_the_app(
    memory_checkout: tuple[Path, Path], tmp_path: Path, gateway_data_plane: None
) -> None:
    source, root = memory_checkout
    before = run_git("rev-parse", "HEAD", cwd=root)
    run_git("checkout", "-b", "consolidated", cwd=source)
    after = commit_note(source, "after refresh")
    run_git("push", "origin", "consolidated", cwd=source)
    other = tmp_path / "unrelated"
    init_local_repo("main", other)
    other_head = run_git("rev-parse", "HEAD", cwd=other)
    boot = snapshot_process_config()
    boot.set_field("memory_keep_local", True)
    config = replace(gateway_config_from_boot(boot), memory_root=root)

    with gateway_test_client(config=config, boot=boot) as client:
        initial_note = client.get("/api/memory/note", params={"path": "note.md"})
        assert initial_note.status_code == 200
        assert "before refresh" in initial_note.json()["body"]
        kept = client.post("/api/memory/refresh")
        assert kept.status_code == 200
        assert kept.json()["head"] == before
        boot.set_field("memory_keep_local", False)
        boot.set_field("track_branch", "consolidated")
        refreshed = client.post("/api/memory/refresh")
        assert refreshed.status_code == 200
        assert refreshed.json()["head"] == after
        note = client.get("/api/memory/note", params={"path": "note.md"})
        assert note.status_code == 200
        assert "after refresh" in note.json()["body"]

    assert run_git("rev-parse", "HEAD", cwd=root) == after
    assert run_git("rev-parse", "HEAD", cwd=other) == other_head


def test_refresh_keeps_serving_while_git_waits(
    memory_checkout: tuple[Path, Path], tmp_path: Path, gateway_data_plane: None
) -> None:
    source, root = memory_checkout
    after = commit_note(source, "after refresh")
    run_git("push", "origin", "main", cwd=source)
    started = tmp_path / "hook-started"
    released = tmp_path / "request-served"
    observed = tmp_path / "hook-result"
    hook = root / ".git" / "hooks" / "post-merge"
    hook.write_text(
        f"#!{sys.executable}\n"
        "import time\nfrom pathlib import Path\n"
        f"Path({str(started)!r}).touch()\n"
        "deadline = time.monotonic() + 10\n"
        f"while not Path({str(released)!r}).exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        f"Path({str(observed)!r}).write_text(str(Path({str(released)!r}).exists()))\n"
    )
    hook.chmod(0o700)
    boot = snapshot_process_config()
    boot.set_field("memory_keep_local", False)
    boot.set_field("track_branch", "main")
    config = replace(gateway_config_from_boot(boot), memory_root=root)

    with gateway_test_client(config=config, boot=boot) as client, ThreadPoolExecutor() as executor:
        refresh = executor.submit(client.post, "/api/memory/refresh")
        deadline = time.monotonic() + 10
        while not started.exists() and time.monotonic() < deadline:
            if refresh.done():
                refresh.result()
                raise AssertionError("refresh completed without running the Git hook")
            time.sleep(0.01)
        try:
            assert started.exists(), "Git did not reach the post-merge hook"
            response = client.get("/api/memory/note", params={"path": "note.md"})
            assert response.status_code == 200
            assert "after refresh" in response.json()["body"]
        finally:
            released.touch()
        assert refresh.result(timeout=15).json()["head"] == after

    assert observed.read_text() == "True", "the gateway loop stalled while Git waited"

"""The candidate image's side of the handoff: `python -m cli.release_handoff`.

The entry runs only as the image its request names as executor, reads the
exact request (a path, or stdin for the ops kind), and dispatches `submit` to
the home release journal and `receipt` / `preflight` to the fleet release's
unit answers (their behavior: tests/lifecycle/release_fleet/test_entries.py).
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import cli.release_handoff.__main__ as entry
from tests.lifecycle.handoff.conftest import Store


@pytest.fixture
def submitted(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    calls: list[bytes] = []

    def submit(encoded: bytes) -> int:
        calls.append(encoded)
        return 0

    monkeypatch.setattr("cli.release_transition.submit.run", submit)
    return calls


def _as_image(monkeypatch: pytest.MonkeyPatch, store: Store, label: str = "executor") -> None:
    reference = store.executor if label == "executor" else store.previous
    root = store.home / "releases" / reference.artifact_digest
    monkeypatch.setattr(entry, "_code_root", lambda: root / "venv/lib/python3.12/site-packages")


def test_submit_runs_the_journal_submission_on_the_exact_request(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, submitted: list[bytes]
) -> None:
    _as_image(monkeypatch, store)
    request = store.request()
    path = tmp_path / "request.json"
    path.write_bytes(request)
    assert entry.main(["submit", str(path)]) == 0
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(request)))
    assert entry.main(["submit", "-"]) == 0
    assert submitted == [request, request]


def test_the_entry_drops_a_launcher_profile_like_the_cli(
    store: Store, monkeypatch: pytest.MonkeyPatch, submitted: list[bytes]
) -> None:
    """Run by an ops server, the entry does not inherit that service's process
    profile: it records it exactly as `ava` does and runs settings-full."""
    _as_image(monkeypatch, store)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(store.request())))
    with patch.dict(os.environ, {"AVA_PROCESS_PROFILE": "ops"}):
        assert entry.main(["submit", "-"]) == 0
        assert "AVA_PROCESS_PROFILE" not in os.environ
        assert os.environ["AVA_LAUNCHER_PROFILE"] == "ops"
    assert len(submitted) == 1


def test_the_entry_runs_only_as_the_named_executor(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    submitted: list[bytes],
) -> None:
    """A request naming image B never runs as image A's code (or from source)."""
    _as_image(monkeypatch, store, "previous")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(store.request())))
    assert entry.main(["submit", "-"]) == 2
    assert "runs only as the image the request names" in capsys.readouterr().err
    assert submitted == []


@pytest.mark.parametrize("name", ["receipt", "preflight"])
def test_the_unit_answers_refuse_a_document_they_cannot_answer(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    submitted: list[bytes],
    name: str,
) -> None:
    """A fleet request is no unit's preflight, and this fixture image carries no
    migration inventory for a receipt: each refuses, and nothing is submitted."""
    _as_image(monkeypatch, store)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(store.request())))
    assert entry.main([name, "-"]) == 2
    assert f"release {name} refused" in capsys.readouterr().err
    assert submitted == []


@pytest.mark.parametrize("argv", [[], ["submit"], ["migrate", "-"], ["submit", "-", "extra"]])
def test_the_frozen_entry_module_refuses_other_argv(tmp_path: Path, argv: list[str]) -> None:
    """The v1 module name resolves in the isolated interpreter mode the handoff uses."""
    result = subprocess.run(  # noqa: S603 — this interpreter, the fixed entry module
        [sys.executable, "-I", "-B", "-X", "utf8", "-m", "cli.release_handoff", *argv],
        cwd=tmp_path,
        input=b"",
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2
    assert b"usage: python -m cli.release_handoff {receipt,preflight,submit}" in result.stderr

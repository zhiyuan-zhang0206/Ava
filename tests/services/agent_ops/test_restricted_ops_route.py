"""Tests for the restricted observer's /ops allowlist and its dispatch child.

Covers:
- bootstrap.ops_route: admitted kinds relayed through the child; everything else
  refused as a failed op without spawning one; 400s mirror the daemon's
- bootstrap.run_dispatch_child: a child that times out / exits non-zero /
  prints no envelope degrades to the daemon's failed-envelope shape
- services.agent_ops.dispatch_child: one envelope in, one {status, result} out
- daemon.dispatch_once: keyed envelopes route through the idempotent pass,
  unkeyed ones through dispatch, and the process-local pool is always closed
- the restricted observer module stays importable without shared.config
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from services.agent_ops import bootstrap, daemon, dispatch_child
from shared.op_envelope import OpEnvelope

_REPO = Path(__file__).resolve().parents[3]


def _body(kind: str, *, key: str | None = None) -> bytes:
    envelope: dict[str, object] = {"kind": kind, "payload": {}}
    if key is not None:
        envelope["idempotency_key"] = key
    return json.dumps(envelope).encode()


# ─── the /ops route ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admitted_kinds_relay_through_the_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, Path]] = []

    def _fake_child(envelope: OpEnvelope, home: Path) -> tuple[str, dict[str, object]]:
        calls.append((envelope.kind, home))
        return "completed", {"session": "ava-updater"}

    monkeypatch.setattr(bootstrap, "run_dispatch_child", _fake_child)
    route = bootstrap.ops_route(tmp_path)
    for kind in ("cluster_bootstrap_hop", "cluster_normal_continue"):
        status, body, content_type = await route(_body(kind))
        assert (status, content_type) == (200, "application/json")
        answer = json.loads(body)
        assert answer == {"status": "completed", "result": {"session": "ava-updater"}}
    assert calls == [("cluster_bootstrap_hop", tmp_path), ("cluster_normal_continue", tmp_path)]


@pytest.mark.asyncio
async def test_other_kinds_are_refused_without_a_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> tuple[str, dict[str, object]]:
        raise AssertionError("a non-admitted kind must never reach a child")

    monkeypatch.setattr(bootstrap, "run_dispatch_child", _boom)
    status, body, _ = await bootstrap.ops_route(tmp_path)(_body("cluster_update"))
    assert status == 200
    answer = json.loads(body)
    assert answer["status"] == "failed"
    assert "not admitted" in answer["result"]["error"]


@pytest.mark.asyncio
async def test_malformed_bodies_are_400_like_the_daemon(tmp_path: Path) -> None:
    """Exact-equality pins on both 400 bodies: same text the daemon would send."""
    route = bootstrap.ops_route(tmp_path)
    raw = b"{not json"
    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:
        json_expected = {"error": f"invalid JSON body: {exc}"}
    else:  # pragma: no cover — the fixture must stay invalid JSON.
        raise AssertionError("fixture is not invalid JSON")
    status, body, _ = await route(raw)
    assert status == 400
    assert json.loads(body) == json_expected

    bad = json.dumps({"payload": {}}).encode()
    try:
        OpEnvelope.model_validate(json.loads(bad))
    except ValidationError as exc:
        envelope_expected = {"error": f"body must be {{kind: str, payload: dict}}: {exc}"}
    else:  # pragma: no cover — the fixture must stay an invalid envelope.
        raise AssertionError("fixture is not an invalid envelope")
    status, body, _ = await route(bad)
    assert status == 400
    assert json.loads(body) == envelope_expected


# ─── the child runner ────────────────────────────────────────────────────────


class _Completed:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_child_exit_failure_degrades_to_a_failed_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _run(*_args: object, **_kwargs: object) -> _Completed:
        return _Completed(3, stderr=b"boom")

    monkeypatch.setattr(bootstrap.subprocess, "run", _run)
    status, result = bootstrap.run_dispatch_child(
        OpEnvelope(kind="cluster_bootstrap_hop"), tmp_path
    )
    assert status == "failed"
    assert "exited 3" in str(result["error"])
    assert "boom" in str(result["error"])


def test_child_timeout_degrades_to_a_failed_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _timeout(*_args: object, **_kwargs: object) -> Any:
        raise subprocess.TimeoutExpired(cmd="dispatch_child", timeout=1)

    monkeypatch.setattr(bootstrap.subprocess, "run", _timeout)
    status, result = bootstrap.run_dispatch_child(
        OpEnvelope(kind="cluster_bootstrap_hop"), tmp_path
    )
    assert status == "failed"
    assert "exceeded" in str(result["error"])


def test_child_garbage_output_degrades_to_a_failed_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _run(*_args: object, **_kwargs: object) -> _Completed:
        return _Completed(0, stdout=b"noise")

    monkeypatch.setattr(bootstrap.subprocess, "run", _run)
    status, result = bootstrap.run_dispatch_child(
        OpEnvelope(kind="cluster_bootstrap_hop"), tmp_path
    )
    assert status == "failed"
    assert "no envelope" in str(result["error"])


def test_child_envelope_is_relayed_verbatim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stdout = json.dumps({"status": "failed", "result": {"error": "refused"}}).encode()

    def _run(*_args: object, **_kwargs: object) -> _Completed:
        return _Completed(0, stdout=stdout)

    monkeypatch.setattr(bootstrap.subprocess, "run", _run)
    status, result = bootstrap.run_dispatch_child(
        OpEnvelope(kind="cluster_normal_continue"), tmp_path
    )
    assert (status, result) == ("failed", {"error": "refused"})


# ─── the child entry ─────────────────────────────────────────────────────────


class _FakeStdin:
    def __init__(self, payload: bytes) -> None:
        self.buffer = io.BytesIO(payload)


def test_dispatch_child_answers_one_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _fake_once(
        kind: str, _payload: dict[str, object], *, idempotency_key: str | None
    ) -> tuple[str, dict[str, object]]:
        return "completed", {"kind": kind, "key": idempotency_key}

    monkeypatch.setattr(daemon, "dispatch_once", _fake_once)
    monkeypatch.setattr(
        sys,
        "stdin",
        _FakeStdin(
            json.dumps(
                {"kind": "cluster_bootstrap_hop", "payload": {}, "idempotency_key": "k1"}
            ).encode()
        ),
    )
    assert dispatch_child.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "completed",
        "result": {"kind": "cluster_bootstrap_hop", "key": "k1"},
    }


def test_dispatch_child_rejects_a_bad_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(b"not json"))
    assert dispatch_child.main() == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["status"] == "failed"
    assert "invalid envelope" in answer["result"]["error"]


# ─── the one-shot dispatch ───────────────────────────────────────────────────


def test_dispatch_once_routes_keyed_and_unkeyed(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[str] = []

    class _Pool:
        def close(self) -> None:
            closed.append("closed")

    monkeypatch.setattr(daemon, "_open_db_pool", _Pool)

    async def _dispatch(kind: str, payload: dict[str, Any]) -> tuple[str, dict[str, object]]:
        return "completed", {"via": "dispatch", "kind": kind}

    async def _idempotent(
        kind: str, payload: dict[str, Any], key: str, pool: object
    ) -> tuple[str, dict[str, object]]:
        return "completed", {"via": "idempotent", "key": key}

    monkeypatch.setattr(daemon, "_dispatch", _dispatch)
    monkeypatch.setattr(daemon, "_dispatch_idempotent", _idempotent)

    assert daemon.dispatch_once("cluster_bootstrap_hop", {}, idempotency_key=None) == (
        "completed",
        {"via": "dispatch", "kind": "cluster_bootstrap_hop"},
    )
    assert daemon.dispatch_once("cluster_bootstrap_hop", {}, idempotency_key="k1") == (
        "completed",
        {"via": "idempotent", "key": "k1"},
    )
    assert closed == ["closed", "closed"]
    assert daemon._db_pool is None


# ─── the import boundary ─────────────────────────────────────────────────────


def test_restricted_observer_import_is_settings_free() -> None:
    """The observer's whole point: importing it must not pull ordinary Settings."""
    probe = (
        "import sys; import services.agent_ops.bootstrap; "
        "assert 'shared.config' not in sys.modules, sorted(m for m in sys.modules if 'config' in m)"
    )
    completed = subprocess.run(  # noqa: S603 -- fixed argv, test-only probe.
        [sys.executable, "-B", "-c", probe],
        cwd=_REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

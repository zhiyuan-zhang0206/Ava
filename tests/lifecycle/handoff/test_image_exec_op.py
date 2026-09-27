"""The `release_image_exec` ops kind: a unit runs one entry of a verified image.

The unit's ops server (still the previous image) verifies the named image in
its own store, requires the request's envelope to name this unit and that
image, and runs the fixed v1 entry on the exact request bytes with bounded
time, returning the entry's JSON object and changing nothing else.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import psutil
import pytest
from pydantic import ValidationError

from ops import ops_cluster
from ops.rpc_schemas import is_op_kind
from services.agent_ops.dispatch_sync import dispatch_sync
from shared.api_contracts.release_handoff import (
    HandoffRefusedError,
    ReleaseImageExecPayload,
    ReleaseImageRef,
)
from tests.lifecycle.handoff.conftest import Store, entry_argv_tail

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the recording interpreter is POSIX")


@pytest.fixture
def unit(store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    """The ops daemon's view of this unit: its home and its machine name."""
    monkeypatch.setattr(ops_cluster, "ava_home", lambda: store.home)
    monkeypatch.setattr(ops_cluster, "machine_name", lambda: "unit-a")
    return store


def _payload(store: Store, entry: str, request: bytes, **image: str) -> dict[str, Any]:
    return {
        "entry": entry,
        "image": {**store.executor.model_dump(), **image},
        "request": base64.b64encode(request).decode(),
    }


@pytest.mark.parametrize("entry", ["receipt", "preflight", "submit"])
def test_the_op_runs_the_entry_on_the_exact_request_bytes(unit: Store, entry: str) -> None:
    assert is_op_kind("release_image_exec")
    request = unit.request()
    status, result = dispatch_sync("release_image_exec", _payload(unit, entry, request), pool=None)
    assert (status, result) == ("completed", {"entry": entry, "result": {"ok": True}})
    cwd, home, *argv = unit.recorded()
    assert argv == entry_argv_tail(entry, "-")
    assert Path(cwd) == unit.image.cwd.resolve() and home == str(unit.home)
    assert (unit.record.parent / f"{unit.record.name}.stdin").read_bytes() == request


def test_the_wire_payload_is_closed(unit: Store) -> None:
    payload = _payload(unit, "submit", unit.request())
    with pytest.raises(ValidationError):
        ReleaseImageExecPayload.model_validate({**payload, "timeout": 5})
    with pytest.raises(ValidationError):
        ReleaseImageExecPayload.model_validate({**payload, "entry": "migrate"})
    with pytest.raises(ValidationError):
        dispatch_sync(
            "release_image_exec",
            {**payload, "image": {**payload["image"], "abi_tag": {}}},
            pool=None,
        )
    assert not (unit.record.parent / f"{unit.record.name}.argv").exists()


def _run(store: Store, request: bytes, **kwargs: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "home": store.home,
        "machine": "unit-a",
        "entry": "preflight",
        "image": ReleaseImageRef(**store.executor.model_dump()),
        "request": request,
    }
    return ops_cluster.run_release_entry(**(arguments | kwargs))


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("another home", "the request belongs to"),
        ("another machine", "names machine 'unit-b'"),
        ("another image", "another executor than the image to run"),
        ("no envelope", "no v1 handoff envelope"),
    ],
)
def test_the_op_refuses_a_request_for_another_unit_or_image(
    store: Store, tmp_path: Path, change: str, reason: str
) -> None:
    request, kwargs = store.request(), {}
    if change == "another home":
        kwargs = {"home": tmp_path}
    elif change == "another machine":
        request = store.request(machine="unit-b")
    elif change == "another image":
        kwargs = {"image": ReleaseImageRef(**store.previous.model_dump())}
    else:
        request = b'{"version": 1}'
    with pytest.raises(HandoffRefusedError, match=reason):
        _run(store, request, **kwargs)
    assert not (store.record.parent / f"{store.record.name}.argv").exists()


@pytest.mark.parametrize(
    ("environment", "reason"),
    [
        ({"HANDOFF_EXIT": "3", "HANDOFF_STDERR": "entry said no"}, "exited 3: entry said no"),
        ({"HANDOFF_STDOUT": "not json"}, "printed no JSON document: not json"),
        ({"HANDOFF_STDOUT": "[1, 2]"}, "non-object JSON document"),
    ],
)
def test_a_failed_entry_is_a_failed_op(
    store: Store, monkeypatch: pytest.MonkeyPatch, environment: dict[str, str], reason: str
) -> None:
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(HandoffRefusedError, match=reason):
        _run(store, store.request())


def test_an_entry_past_its_bound_is_killed(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HANDOFF_SLEEP", "30")
    with pytest.raises(HandoffRefusedError, match=r"did not finish within 0\.5s"):
        _run(store, store.request(), timeout_s=0.5)
    # The entry was the only process (the script execs `sleep`), and the bound killed it.
    leftovers = [
        child
        for child in psutil.Process().children(recursive=True)
        if child.cmdline()[:2] == ["sleep", "30"]
    ]
    assert leftovers == []


def test_a_request_that_is_not_base64_refuses(unit: Store) -> None:
    payload = _payload(unit, "submit", unit.request()) | {"request": "not base64!"}
    with pytest.raises(HandoffRefusedError, match="not valid base64"):
        dispatch_sync("release_image_exec", payload, pool=None)

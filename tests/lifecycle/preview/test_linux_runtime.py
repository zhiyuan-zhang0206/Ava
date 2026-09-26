"""Image identity never comes from a selector or substitutes for native custody."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from cli.release_prepare import Preparation
from cli.release_prepare import prepare as preparation
from scripts.preview import linux_observer as observer
from scripts.preview import linux_runtime as runtime
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease
from tests.lifecycle.preparation.test_preparation import _assemble
from tests.lifecycle.preparation.test_preparation import request_fixture as request_fixture


@pytest.fixture
def receipt_path(request_fixture: Preparation, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(preparation, "prepare_release", _assemble)
    preparation.prepare_image(request_fixture)
    return request_fixture.work / "receipt.json"


def test_prepared_receipt_verifies_image_despite_unreadable_moving_selector(
    request_fixture: Preparation, receipt_path: Path
) -> None:
    run = request_fixture.store.parent.parent
    expected = runtime.expected_runtime(run, receipt_path)
    assert expected.image is not None and expected.evidence is not None
    assert expected.evidence["source_commit"] == request_fixture.commit
    assert expected.argv(run)[:7] == [
        str(expected.interpreter),
        "-I",
        "-B",
        "-X",
        "utf8",
        "-m",
        "services.ava_root",
    ]
    assert (
        request_fixture.store / "current-release"
    ).read_text() == "unreadable selector sentinel\n"
    # A selector cannot silently switch ordinary source observation into image mode.
    source = runtime.expected_runtime(run, None)
    assert source.image is None and source.interpreter == run / "source/.venv/bin/python"


@pytest.mark.parametrize("field", ["interpreter", "cwd", "platform", "source_tree"])
def test_receipt_rejects_paths_or_identity_not_supported_by_verified_bytes(
    request_fixture: Preparation, receipt_path: Path, field: str
) -> None:
    value = json.loads(receipt_path.read_text())
    if field in {"interpreter", "cwd"}:
        value["image"][field] = value["image"]["root"] + "/another-path"
    elif field == "platform":
        value["image"][field] = "foreign-platform"
    else:
        value["source"][field] = "f" * 40
    receipt_path.write_text(json.dumps(value))
    with pytest.raises(RuntimeError):
        runtime.expected_runtime(request_fixture.store.parent.parent, receipt_path)


def test_other_home_receipt_and_changed_image_are_rejected(
    request_fixture: Preparation, receipt_path: Path
) -> None:
    run = request_fixture.store.parent.parent
    with pytest.raises(RuntimeError, match="another preview home"):
        runtime.expected_runtime(run / "other", receipt_path)
    expected = runtime.expected_runtime(run, receipt_path)
    expected.interpreter.write_bytes(b"changed bytes")
    with pytest.raises(ReleaseRejectedError, match="hash mismatch"):
        runtime.expected_runtime(run, receipt_path)


@pytest.mark.parametrize("changed", ["digest", "commit"])
def test_bound_preflight_rejects_substituted_receipt_or_requested_source(
    request_fixture: Preparation, receipt_path: Path, changed: str
) -> None:
    encoded = receipt_path.read_bytes()
    digest = hashlib.sha256(encoded).hexdigest()
    commit = request_fixture.commit
    if changed == "digest":
        receipt_path.write_bytes(encoded + b" ")
    else:
        commit = "f" * 40
    with pytest.raises(RuntimeError, match=r"(digest changed|requested source commit)"):
        runtime.bound_runtime(request_fixture.store.parent.parent, receipt_path, digest, commit)


def test_bound_preflight_verifies_same_parsed_bytes_even_if_path_changes_during_verification(
    request_fixture: Preparation, receipt_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    encoded = receipt_path.read_bytes()
    verify = runtime.verify_release

    def changed(*args: Any, **kwargs: Any) -> VerifiedRelease:
        receipt_path.write_text("changed after one bound read")
        return verify(*args, **kwargs)

    monkeypatch.setattr(runtime, "verify_release", changed)
    result = runtime.bound_runtime(
        request_fixture.store.parent.parent,
        receipt_path,
        hashlib.sha256(encoded).hexdigest(),
        request_fixture.commit,
    )
    assert result.evidence is not None
    assert result.evidence["receipt_sha256"] == hashlib.sha256(encoded).hexdigest()
    assert result.evidence["source_commit"] == request_fixture.commit


def _expected(run: Path, *, image: bool) -> runtime.ExpectedRuntime:
    (run / "home").mkdir()
    (run / "home/.env").write_text("AVA_SERVICE_PATH=/usr/bin:/bin\n")
    if image:
        root = run / "home/releases" / ("a" * 64)
        interpreter, cwd = root / "venv/bin/python", root / "venv/lib/python3.12/site-packages"
        interpreter.parent.mkdir(parents=True)
        cwd.mkdir(parents=True)
        interpreter.write_bytes(b"inert native metadata fixture")
        verified = VerifiedRelease("a" * 64, "b" * 64, root, interpreter, cwd)
        return runtime.ExpectedRuntime(interpreter, cwd, verified)
    expected = runtime.expected_runtime(run, None)
    expected.interpreter.parent.mkdir(parents=True)
    expected.interpreter.write_bytes(b"inert native metadata fixture")
    return expected


@pytest.mark.parametrize("image", [False, True])
@pytest.mark.parametrize(
    "changed",
    [
        None,
        "argv",
        "cwd",
        "executable",
        "AVA_HOME",
        "AVA_CLUSTER_REGISTRY",
        "VIRTUAL_ENV",
        "AVA_SERVICE_PATH",
        "PATH",
        "PYTHONPATH",
        "isolation",
    ],
)
def test_root_runtime_metadata_and_environment_must_match_even_with_live_birth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str | None, *, image: bool
) -> None:
    run = tmp_path.resolve()
    expected = _expected(run, image=image)
    environment = expected.environment(run, "/usr/bin:/bin") | {"PRIVATE_KEY": "never-print-this"}
    native: dict[str, Any] = {
        "argv": expected.argv(run),
        "cwd": str(expected.cwd),
        "executable": str(expected.interpreter.resolve()),
    }
    if changed == "argv":
        native["argv"][0] = "/another/image/venv/bin/python"
    elif changed == "isolation":
        if image:
            native["argv"].remove("-I")
        else:
            native["argv"].append("--unexpected-option")
    elif changed in {"cwd", "executable"}:
        native[changed] = "/another/image"
    elif changed == "PATH":
        environment[changed] += ":/foreign/extra/bin"
    elif changed is not None:
        environment[changed] = "/another/image"

    class Process:
        def __init__(self, _pid: int):
            pass

        def environ(self) -> dict[str, str]:
            return environment

        def cmdline(self) -> list[str]:
            return native["argv"]

        def cwd(self) -> str:
            return native["cwd"]

        def exe(self) -> str:
            return native["executable"]

    def live(_owner: observer.OwnedProcess) -> bool:
        return True

    monkeypatch.setattr(observer.psutil, "Process", Process)
    monkeypatch.setattr(observer.OwnedProcess, "live", live)
    result: observer.Report = {}
    owner = {"pid": 500, "birth": 100.0, "starttime": 1000}
    if changed is not None:
        with pytest.raises(RuntimeError):
            observer._observe_path(run, owner, result, expected)
    else:
        observer._observe_path(run, owner, result, expected)
        assert result["root_native"]["environment_sha256"] == runtime.environment_digest(
            environment
        )
        assert result["service_path"]["root_path"] == environment["PATH"]
    assert "never-print-this" not in json.dumps(result)

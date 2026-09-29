"""Admission verifies retained bytes; equal baseline names do not hide SQL changes."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from cli.release_fleet.request import FleetRequest
from cli.release_transition import request as request_module
from cli.release_transition.request import ReleaseRef, verify_pair
from shared.deploy.release.runtime_release import (
    MANIFEST_VERSION,
    ReleaseRejectedError,
    VerifiedRelease,
)
from shared.runtime_abi import AbiTag, current_abi

_SITE = "venv/lib/python3.12/site-packages"
_UP = "20260926T000000_example.sql"
_DOWN = "20260926T000000_example.down.sql"
_SQL = {_UP: b"SELECT 1;\n", _DOWN: b"SELECT 2;\n"}
_BASELINE = b"CREATE TABLE example (id bigint);\n"


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _image(
    home: Path,
    label: str,
    *,
    sql: dict[str, bytes] | None = None,
    baseline: bytes = _BASELINE,
    duplicate_sql: dict[str, bytes] | None = None,
) -> ReleaseRef:
    """Create a complete small image; tests never execute its inert interpreter."""
    artifact = _digest(label.encode())
    commit = _digest((label + "-commit").encode())[:40]
    schema = _digest(baseline)
    root = home / "releases" / artifact
    root.mkdir(parents=True)
    files = {
        "venv/bin/python": b"inert interpreter fixture\n",
        f"{_SITE}/db/schema.sql": baseline,
        f"{_SITE}/shared/release-build.json": _canonical(
            {
                "version": 1,
                "source_commit": commit,
                "source_tree": "1" * 40,
                "source_archive_digest": "2" * 64,
                "schema_digest": schema,
                "applied_names": sorted(["__baseline__", _UP.removesuffix(".sql")]),
            }
        ),
    }
    for name, contents in (_SQL if sql is None else sql).items():
        files[f"{_SITE}/migrations/{name}"] = contents
    if duplicate_sql is not None:
        for name, contents in duplicate_sql.items():
            files[f"{_SITE.replace('/lib/', '/lib64/')}/migrations/{name}"] = contents
    for name, contents in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    manifest = _canonical(
        {
            "version": MANIFEST_VERSION,
            "artifact_digest": artifact,
            "abi_tag": current_abi().to_json(),
            "platform": "provenance-only-platform-string",
            "schema_digest": schema,
            "interpreter": "venv/bin/python",
            "cwd": _SITE,
            "files": {name: _digest(contents) for name, contents in files.items()},
        }
    )
    (root / "manifest.json").write_bytes(manifest)
    return ReleaseRef(
        artifact_digest=artifact,
        manifest_digest=_digest(manifest),
        schema_digest=schema,
        source_commit=commit,
    )


def _request(home: Path, previous: ReleaseRef, candidate: ReleaseRef) -> FleetRequest:
    return FleetRequest(
        id=uuid4(),
        home=str(home),
        registry=str(home.parent / "clusters.json"),
        created_at=datetime.now(UTC),
        machine="test-unit",
        previous=previous,
        candidate=candidate,
        executor=candidate,
        configuration_digest="f" * 64,
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path.resolve() / "home"
    path.mkdir()
    return path


def _files(home: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        str(path.relative_to(home)): (
            path.read_bytes(),
            path.stat().st_ino,
            path.stat().st_mtime_ns,
        )
        for path in home.rglob("*")
        if path.is_file()
    }


def test_exact_pair_admission_is_read_only_and_replayable(home: Path) -> None:
    previous = _image(home, "previous")
    candidate = _image(home, "candidate", duplicate_sql=_SQL)
    request = _request(home, previous, candidate)
    before = _files(home)
    pair = verify_pair(request)
    assert pair == verify_pair(FleetRequest.model_validate_json(request.model_dump_json()))
    assert (pair[0].digest, pair[1].digest) == (previous.artifact_digest, candidate.artifact_digest)
    assert _files(home) == before
    assert not request.path.parent.exists()


def _patched(tag: AbiTag) -> AbiTag:
    """The same host after an OS patch/upgrade: only the release floor rises."""
    if tag.os == "linux":
        major, minor = tag.floor()
        return dataclasses.replace(tag, libc_version=f"{major}.{minor + 1}")
    return dataclasses.replace(tag, macos=str(tag.floor()[0] + 1))


def test_verification_observes_the_host_now_not_a_captured_platform(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boot, selection and stage all verify through `ReleaseRef.verify`."""
    request = _request(home, _image(home, "previous"), _image(home, "candidate"))
    host = current_abi()
    monkeypatch.setattr(request_module, "current_abi", lambda: _patched(host))
    assert verify_pair(request)[1].digest == request.candidate.artifact_digest
    foreign = dataclasses.replace(host, arch="riscv64")
    monkeypatch.setattr(request_module, "current_abi", lambda: foreign)
    with pytest.raises(ReleaseRejectedError, match="incompatible with this host"):
        request.candidate.verify(home)
    older = dataclasses.replace(host, python="cpython-311")
    monkeypatch.setattr(request_module, "current_abi", lambda: older)
    with pytest.raises(ReleaseRejectedError, match="python"):
        verify_pair(request)


class _AdmittedError(Exception):
    """Stops the boot at admission, before Settings load."""


def test_boot_checks_the_booting_host_before_admission(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reboot after an OS patch still boots; a foreign host refuses before Settings."""
    from cli import start_runtime
    from cli.release_transition import boot

    image = _image(home, "candidate")
    registry = home.parent / "clusters.json"
    admitted: list[str] = []

    def admit(_home: Path, verified: VerifiedRelease, **_facts: str) -> None:
        admitted.append(verified.digest)
        raise _AdmittedError

    monkeypatch.setattr(start_runtime, "admit_release", admit)
    # The boot entry exports its home; keep that out of this test process.
    monkeypatch.setattr(boot, "os", SimpleNamespace(environ={}))
    host = current_abi()
    monkeypatch.setattr(
        request_module, "current_abi", lambda: dataclasses.replace(host, arch="riscv64")
    )
    with pytest.raises(ReleaseRejectedError, match="incompatible with this host"):
        boot.start_image(home, registry, image)
    assert admitted == []
    monkeypatch.setattr(request_module, "current_abi", lambda: _patched(host))
    with pytest.raises(_AdmittedError):
        boot.start_image(home, registry, image)
    assert admitted == [image.artifact_digest]


@pytest.mark.parametrize("change", ["up", "down", "added", "removed"])
def test_same_baseline_changed_migration_sql_refuses_before_any_intent(
    home: Path, change: str
) -> None:
    sql = dict(_SQL)
    if change == "up":
        sql[_UP] = b"SELECT 999;\n"
    elif change == "down":
        sql[_DOWN] = b"DROP TABLE example;\n"
    elif change == "added":
        sql["20260926T000001_another.sql"] = b"SELECT 3;\n"
    else:
        del sql[_DOWN]
    previous, candidate = _image(home, "previous"), _image(home, "candidate", sql=sql)
    assert previous.schema_digest == candidate.schema_digest
    request = _request(home, previous, candidate)
    before = _files(home)
    with pytest.raises(ReleaseRejectedError, match="changed migration SQL"):
        verify_pair(request)
    assert _files(home) == before
    assert not request.path.parent.exists()


@pytest.mark.parametrize("inventory", ["missing", "conflicting-copy"])
def test_incomplete_or_disagreeing_sql_inventory_is_not_same_schema(
    home: Path, inventory: str
) -> None:
    previous = _image(home, "previous")
    candidate = _image(
        home,
        "candidate",
        sql={} if inventory == "missing" else None,
        duplicate_sql={_UP: b"SELECT 999;\n", _DOWN: _SQL[_DOWN]}
        if inventory == "conflicting-copy"
        else None,
    )
    request = _request(home, previous, candidate)
    with pytest.raises(ReleaseRejectedError, match="missing or inconsistent migration"):
        verify_pair(request)
    assert not request.path.parent.exists()


def test_different_baseline_refuses_even_when_migration_scripts_match(home: Path) -> None:
    request = _request(
        home,
        _image(home, "previous"),
        _image(home, "candidate", baseline=b"CREATE TABLE another (id bigint);\n"),
    )
    with pytest.raises(ReleaseRejectedError, match="schema-changing release"):
        verify_pair(request)
    assert not request.path.parent.exists()


@pytest.mark.parametrize("which", ["previous", "candidate"])
def test_source_commit_is_bound_to_each_actual_image(home: Path, which: str) -> None:
    request = _request(home, _image(home, "previous"), _image(home, "candidate"))
    changed = getattr(request, which).model_copy(update={"source_commit": "0" * 40})
    updates = {which: changed}
    if which == "candidate":
        updates["executor"] = changed
    request = FleetRequest.model_validate(request.model_dump() | updates)
    with pytest.raises(ReleaseRejectedError, match="target commit or schema"):
        verify_pair(request)


@pytest.mark.parametrize("which", ["previous", "candidate"])
def test_changed_sql_bytes_cannot_reuse_a_verified_manifest(home: Path, which: str) -> None:
    request = _request(home, _image(home, "previous"), _image(home, "candidate"))
    reference = getattr(request, which)
    path = home / "releases" / reference.artifact_digest / _SITE / "migrations" / _UP
    path.write_bytes(b"SELECT 'changed after preparation';\n")
    with pytest.raises(ReleaseRejectedError, match="hash mismatch"):
        verify_pair(request)


def test_moving_home_alias_cannot_admit_the_captured_pair(home: Path) -> None:
    request = _request(home, _image(home, "previous"), _image(home, "candidate"))
    alias = home.parent / "home-alias"
    alias.symlink_to(home, target_is_directory=True)
    request = FleetRequest.model_validate(request.model_dump() | {"home": str(alias)})
    with pytest.raises(ReleaseRejectedError, match="canonical"):
        verify_pair(request)
    assert not (home / "updates").exists()


@pytest.mark.parametrize("change", ["separate-executor", "same-image"])
def test_request_cannot_substitute_its_executor_or_reactivate_same_image(
    home: Path, change: str
) -> None:
    request = _request(home, _image(home, "previous"), _image(home, "candidate"))
    updates = (
        {"executor": request.previous}
        if change == "separate-executor"
        else {
            "candidate": request.previous,
            "executor": request.previous,
        }
    )
    with pytest.raises(ValidationError):
        FleetRequest.model_validate(request.model_dump() | updates)


@pytest.mark.parametrize("field", ["home", "registry"])
@pytest.mark.parametrize(
    "value", ["relative/path", "/rejected/../other", "/rejected/trailing/", "/rejected/new\nline"]
)
def test_request_paths_cannot_change_meaning_during_replay(
    home: Path, field: str, value: str
) -> None:
    request = _request(home, _image(home, "previous"), _image(home, "candidate"))
    with pytest.raises(ValidationError, match="release operation paths"):
        FleetRequest.model_validate(request.model_dump() | {field: value})

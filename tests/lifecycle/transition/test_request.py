"""Admission verifies retained bytes; equal baseline names do not hide SQL changes."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from cli.release_transition.request import ReleaseRef, Request, verify_pair
from shared.runtime_release import ReleaseRejectedError

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
            "version": 1,
            "artifact_digest": artifact,
            "platform": "Linux-test",
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


def _request(home: Path, previous: ReleaseRef, candidate: ReleaseRef) -> Request:
    return Request(
        id=uuid4(),
        home=str(home),
        registry=str(home.parent / "clusters.json"),
        created_at=datetime.now(UTC),
        platform_tag="Linux-test",
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
    assert pair == verify_pair(Request.model_validate_json(request.model_dump_json()))
    assert (pair[0].digest, pair[1].digest) == (previous.artifact_digest, candidate.artifact_digest)
    assert _files(home) == before
    assert not request.path.parent.exists()


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
    request = Request.model_validate(request.model_dump() | updates)
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
    request = Request.model_validate(request.model_dump() | {"home": str(alias)})
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
        Request.model_validate(request.model_dump() | updates)


@pytest.mark.parametrize("field", ["home", "registry"])
@pytest.mark.parametrize(
    "value", ["relative/path", "/rejected/../other", "/rejected/trailing/", "/rejected/new\nline"]
)
def test_request_paths_cannot_change_meaning_during_replay(
    home: Path, field: str, value: str
) -> None:
    request = _request(home, _image(home, "previous"), _image(home, "candidate"))
    with pytest.raises(ValidationError, match="release operation paths"):
        Request.model_validate(request.model_dump() | {field: value})

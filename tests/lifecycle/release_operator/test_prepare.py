"""`ava cluster release prepare` wiring: path resolution and refusal paths.

`prepare_image` itself is exercised by
`tests/lifecycle/preparation/test_preparation.py`; these tests cover only
this package's own wiring — work/store derivation under `$AVA_HOME`, the
`--inputs` refusal, and failure surfacing — by stubbing `prepare_image`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli.release_operator import prepare as prepare_module
from cli.release_prepare import Preparation
from shared import paths as shared_paths

_COMMIT = "a" * 40


def _local_inputs_json(tmp_path: Path) -> Path:
    """A syntactically valid LocalInputs document; none of its paths need to
    exist on disk — `prepare_image` is stubbed before any of them is read."""
    body = {
        "version": 1,
        "python": {"root": str(tmp_path / "python"), "digest": "0" * 64},
        "wheelhouse": {"root": str(tmp_path / "wheels"), "digest": "1" * 64},
        "requirements": {"path": str(tmp_path / "requirements.txt"), "digest": "2" * 64},
        "source_lock_digest": "3" * 64,
        "build_constraints": {
            "path": str(tmp_path / "build-constraints.txt"),
            "digest": "4" * 64,
        },
        "uv": {"path": str(tmp_path / "uv"), "digest": "5" * 64},
        "cache_dir": str(tmp_path / "cache"),
    }
    path = tmp_path / "local-inputs.json"
    path.write_text(json.dumps(body))
    return path


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setattr(shared_paths, "ava_home", lambda: path)
    return path


def test_missing_inputs_file_refuses_cleanly(home: Path, tmp_path: Path) -> None:
    code = prepare_module.cmd_release_prepare(
        commit=_COMMIT, inputs=tmp_path / "missing.json", repo=None
    )
    assert code == 2
    assert not (home / "releases").exists()


def test_malformed_inputs_json_refuses_cleanly(home: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    code = prepare_module.cmd_release_prepare(commit=_COMMIT, inputs=bad, repo=None)
    assert code == 2


def test_wiring_resolves_work_and_store_under_home_and_defaults_repo(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Preparation] = {}

    def fake_prepare_image(request: Preparation) -> Preparation:
        captured["request"] = request
        return request  # any Record works: cmd_release_prepare only re-encodes it

    monkeypatch.setattr(prepare_module, "prepare_image", fake_prepare_image)
    inputs = _local_inputs_json(tmp_path)

    code = prepare_module.cmd_release_prepare(commit=_COMMIT, inputs=inputs, repo=None)

    assert code == 0
    request = captured["request"]
    assert request.commit == _COMMIT
    assert request.work == home / "releases" / "work" / _COMMIT
    assert request.store == home / "releases"
    assert request.repo == shared_paths.repo_root()
    # Both `store` and `work`'s parent were bootstrapped as owner-only dirs.
    assert (home / "releases").stat().st_mode & 0o777 == 0o700
    assert (home / "releases" / "work").stat().st_mode & 0o777 == 0o700


def test_explicit_repo_overrides_the_checkout_default(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Preparation] = {}

    def fake_prepare_image(request: Preparation) -> Preparation:
        captured["request"] = request
        return request

    monkeypatch.setattr(prepare_module, "prepare_image", fake_prepare_image)
    repo = tmp_path / "other-repo"
    repo.mkdir()
    inputs = _local_inputs_json(tmp_path)

    code = prepare_module.cmd_release_prepare(commit=_COMMIT, inputs=inputs, repo=repo)

    assert code == 0
    assert captured["request"].repo == repo.resolve()


def test_a_stale_work_directory_fails_the_attempt_rather_than_silently_reusing_it(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_exists(request: Preparation) -> Preparation:
        raise FileExistsError(str(request.work))

    monkeypatch.setattr(prepare_module, "prepare_image", raise_exists)
    inputs = _local_inputs_json(tmp_path)

    code = prepare_module.cmd_release_prepare(commit=_COMMIT, inputs=inputs, repo=None)

    assert code == 1

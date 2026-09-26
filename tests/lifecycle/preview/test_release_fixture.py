"""Scripted proof inputs must match the captured source without copying the whole test tree."""

from __future__ import annotations

import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from cli.release_prepare.models import FileInput
from scripts.preview import release_fixture as fixture
from shared.runtime_release import ApplicationIdentity, ReleaseRejectedError, file_sha256


def _archive(tmp_path: Path, *, invalid: bool = False) -> tuple[Path, ApplicationIdentity]:
    archive = tmp_path / "source.tar"
    with tarfile.open(archive, "w") as stream:
        for name in (*fixture._SOURCE_FILES, "tests/unrelated.py", "shared/production.py"):
            contents = f"# Captured source: {name}\n".encode()
            member = tarfile.TarInfo(name)
            member.size = len(contents)
            if invalid and name == fixture._SOURCE_FILES[0]:
                member.type = tarfile.SYMTYPE
                member.linkname = "/unrelated"
            stream.addfile(member, io.BytesIO(contents))
    identity = ApplicationIdentity(
        version=1,
        source_commit="a" * 40,
        source_tree="b" * 40,
        source_archive_digest=file_sha256(archive),
        schema_digest="c" * 64,
        applied_names=("00000000T000000_baseline",),
    )
    return archive, identity


def _builder(tmp_path: Path, *, mutate: str) -> Path:
    tool = tmp_path / "uv"
    (tmp_path / "fixture_build_input.py").write_text("VALUE = 'captured tool input'\n")
    tool.write_text(
        f"#!{sys.executable}\n"
        "import os,pathlib,sys,zipfile\n"
        "from fixture_build_input import VALUE\n"
        "assert VALUE == 'captured tool input'\n"
        "assert 'PYTHONPATH' not in os.environ and 'AVA_HOME' not in os.environ\n"
        "assert '--offline' in sys.argv and '--require-hashes' in sys.argv\n"
        "out=pathlib.Path(sys.argv[sys.argv.index('--out-dir')+1]); out.mkdir()\n"
        f"mode={mutate!r}\n"
        "if mode=='source': pathlib.Path('tests/e2e/fakes/_chat_model.py').write_text('changed')\n"
        "with zipfile.ZipFile(out/'ava_preview_fixture-0.0.0-py3-none-any.whl','w') as z:\n"
        " for p in pathlib.Path('tests').rglob('*.py'): z.write(p,p.as_posix())\n"
        " if mode=='extra': z.writestr('shared/production.py', 'must not ship')\n"
        " if mode=='duplicate': z.writestr('tests/__init__.py', '')\n"
        " z.writestr('ava_preview_fixture-0.0.0.dist-info/METADATA',"
        " 'Name: ava-preview-fixture\\nVersion: 0.0.0\\n')\n"
    )
    tool.chmod(0o700)
    return tool


@pytest.mark.parametrize("mutate", ["", "source", "extra", "duplicate"])
def test_build_carries_only_exact_captured_fixture_bytes(tmp_path: Path, mutate: str) -> None:
    root = tmp_path.resolve()
    archive, identity = _archive(root)
    cache = root / "cache"
    cache.mkdir()
    constraints = root / "build-requirements.txt"
    constraints.write_text("# inert builder fixture\n")
    uv = _builder(root, mutate=mutate)
    work = root / "work"

    def build() -> fixture.FixtureWheel:
        return fixture.build_fixture(
            archive,
            identity,
            work,
            uv=uv,
            python=Path(sys.executable).resolve(),
            cache_dir=cache,
            build_constraints=FileInput(path=constraints, digest=file_sha256(constraints)),
        )

    if mutate:
        with pytest.raises(ReleaseRejectedError, match="preview fixture wheel"):
            build()
        assert work.is_dir() and not (work / "fixture-receipt.json").exists()
        return
    result = build()
    # A backend's implicit imports must not mutate the supplied tool/Python trees.
    assert not (root / "__pycache__").exists()
    with zipfile.ZipFile(result.wheel) as wheel:
        assert wheel.read("tests/e2e/fakes/_chat_model.py") == (
            b"# Captured source: tests/e2e/fakes/_chat_model.py\n"
        )
        assert wheel.read("tests/__init__.py") == b""
        assert "tests/unrelated.py" not in wheel.namelist()
    receipt = json.loads((work / "fixture-receipt.json").read_text())
    assert receipt["source"] == identity.model_dump(mode="json")
    assert receipt["wheel_digest"] == result.digest == file_sha256(result.wheel)
    with pytest.raises(FileExistsError):
        build()


@pytest.mark.parametrize("bad_input", ["digest", "symlink"])
def test_source_refuses_before_creating_work(tmp_path: Path, bad_input: str) -> None:
    archive, identity = _archive(tmp_path, invalid=bad_input == "symlink")
    if bad_input == "digest":
        archive.write_bytes(b"replaced archive")
    with pytest.raises(ReleaseRejectedError, match=r"(archive|regular source)"):
        fixture._source_bytes(archive, identity)

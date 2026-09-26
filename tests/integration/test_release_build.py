"""Real Git/process preparation, provenance and failed-build isolation regressions."""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

# ruff: noqa: S603 -- fixed Git argv operate only on the generated fixture repository.
from cli import release_build as build
from shared.release_identity import read_application_identity
from shared.runtime_prepare import _materialize_venv_links
from shared.runtime_release import ReleaseRejectedError, VerifiedRelease, file_sha256


@pytest.fixture
def committed_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path.resolve() / "repo"
    repo.mkdir()
    for name in ("shared", "db", "migrations"):
        (repo / name).mkdir()
    (repo / "shared/__init__.py").write_text('VALUE = "committed"\n')
    (repo / "db/schema.sql").write_text("SELECT 1;\n")
    (repo / "migrations/.gitkeep").write_text("")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    return repo, commit


def _constraints(tmp_path: Path) -> Path:
    path = tmp_path.resolve() / "build-tools.txt"
    path.write_text("hatchling==1.0 --hash=sha256:" + "a" * 64 + "\n")
    return path


def _builder(
    tmp_path: Path, *, omit_identity: bool = False, exit_code: int = 0, rewrite_sql: bool = False
) -> Path:
    tool = tmp_path.resolve() / "builder"
    tool.write_text(
        f"#!{sys.executable}\n"
        "import pathlib,sys,zipfile\n"
        f"raise_code={exit_code}\n"
        "if raise_code: raise SystemExit(raise_code)\n"
        "assert '--offline' in sys.argv and '--no-config' in sys.argv and '--require-hashes' in sys.argv\n"
        f"if {rewrite_sql!r}:\n"
        " for p in pathlib.Path('migrations').glob('*.sql'): p.write_text('SELECT 999;\\n')\n"
        "out=pathlib.Path(sys.argv[sys.argv.index('--out-dir')+1]); out.mkdir()\n"
        "with zipfile.ZipFile(out/'ava-fixture.whl','w') as z:\n"
        " for p in pathlib.Path('.').rglob('*'):\n"
        f"  if p.is_file() and not ({omit_identity!r} and p.as_posix()=='shared/release-build.json'):\n"
        "   z.write(p,p.as_posix())\n"
    )
    tool.chmod(0o700)
    return tool


def test_build_uses_commit_not_dirty_working_tree(
    committed_repo: tuple[Path, str], tmp_path: Path
) -> None:
    repo, commit = committed_repo
    (repo / "shared/__init__.py").write_text('VALUE = "dirty"\n')
    (repo / "shared/untracked.py").write_text("SECRET = 'must not ship'\n")
    output = tmp_path.resolve() / "output"
    result = build.build_application(
        repo,
        commit,
        output,
        uv=_builder(tmp_path),
        python=Path(sys.executable),
        build_constraints=_constraints(tmp_path),
    )

    with zipfile.ZipFile(result.wheel) as wheel:
        assert wheel.read("shared/__init__.py") == b'VALUE = "committed"\n'
        assert "shared/untracked.py" not in wheel.namelist()
        identity = json.loads(wheel.read("shared/release-build.json"))
    assert identity["source_commit"] == commit
    assert identity["source_archive_digest"] == file_sha256(output / "source.tar")
    assert result.wheel_digest == file_sha256(result.wheel)
    assert result.applied_names == ("00000000T000000_baseline",)
    assert (repo / "shared/__init__.py").read_text() == 'VALUE = "dirty"\n'
    assert not (repo / "shared/release-build.json").exists()


@pytest.mark.parametrize("target", ["HEAD", "main", "--help", "A" * 40])
def test_mutable_or_invalid_ref_refuses_before_destination(
    committed_repo: tuple[Path, str], tmp_path: Path, target: str
) -> None:
    repo, _ = committed_repo
    output = tmp_path.resolve() / "output"
    with pytest.raises(ReleaseRejectedError, match="exact commit"):
        build.build_application(
            repo,
            target,
            output,
            uv=_builder(tmp_path),
            python=Path(sys.executable),
            build_constraints=_constraints(tmp_path),
        )
    assert not output.exists()


def test_missing_embedded_receipt_refuses_and_retains_build(
    committed_repo: tuple[Path, str], tmp_path: Path
) -> None:
    repo, commit = committed_repo
    output = tmp_path.resolve() / "output"
    with pytest.raises(ReleaseRejectedError, match="exactly one source receipt"):
        build.build_application(
            repo,
            commit,
            output,
            uv=_builder(tmp_path, omit_identity=True),
            python=Path(sys.executable),
            build_constraints=_constraints(tmp_path),
        )
    assert (output / "source.tar").is_file()
    assert not (output / "build-receipt.json").exists()


def test_failed_build_is_not_reused(committed_repo: tuple[Path, str], tmp_path: Path) -> None:
    repo, commit = committed_repo
    output = tmp_path.resolve() / "output"
    uv = _builder(tmp_path, exit_code=9)
    with pytest.raises(subprocess.CalledProcessError):
        build.build_application(
            repo,
            commit,
            output,
            uv=uv,
            python=Path(sys.executable),
            build_constraints=_constraints(tmp_path),
        )
    archive = (output / "source.tar").read_bytes()
    with pytest.raises(FileExistsError):
        build.build_application(
            repo,
            commit,
            output,
            uv=uv,
            python=Path(sys.executable),
            build_constraints=_constraints(tmp_path),
        )
    assert (output / "source.tar").read_bytes() == archive
    assert not (output / "build-receipt.json").exists()


@pytest.fixture
def identity_image(tmp_path: Path) -> tuple[VerifiedRelease, Path]:
    root = tmp_path.resolve() / "image"
    relative = "venv/lib/python3.12/site-packages/shared/release-build.json"
    member = root / relative
    member.parent.mkdir(parents=True)
    identity = {
        "version": 1,
        "source_commit": "a" * 40,
        "source_tree": "b" * 40,
        "source_archive_digest": "c" * 64,
        "schema_digest": "d" * 64,
        "applied_names": ["00000000T000000_baseline"],
    }
    encoded = (json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n").encode()
    member.write_bytes(encoded)
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "platform": "Linux-fixture",
                "schema_digest": "d" * 64,
                "files": {relative: file_sha256(member)},
            }
        )
    )
    image = VerifiedRelease(
        "e" * 64, file_sha256(manifest), root, root / "venv/bin/python", root / "venv"
    )
    return image, member


def test_installed_identity_binds_commit_and_inventory(
    identity_image: tuple[VerifiedRelease, Path],
) -> None:
    image, member = identity_image
    identity = read_application_identity(image, "a" * 40)
    assert identity.applied_names == ("00000000T000000_baseline",)
    with pytest.raises(ReleaseRejectedError, match="target commit or schema"):
        read_application_identity(image, "f" * 40)
    member.write_text(member.read_text().replace('"source_commit":"a', '"source_commit":"b'))
    with pytest.raises(ReleaseRejectedError, match="verified inventory"):
        read_application_identity(image, "a" * 40)


def test_modified_manifest_cannot_bless_another_identity(
    identity_image: tuple[VerifiedRelease, Path],
) -> None:
    image, _ = identity_image
    path = image.root / "manifest.json"
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ReleaseRejectedError, match="manifest changed"):
        read_application_identity(image, "a" * 40)


def _mirrored_identity(image: VerifiedRelease, member: Path) -> tuple[VerifiedRelease, Path]:
    (image.root / "venv/lib64").symlink_to("lib", target_is_directory=True)
    _materialize_venv_links(image.root / "venv")
    mirror = image.root / str(member.relative_to(image.root)).replace("venv/lib/", "venv/lib64/", 1)
    path = image.root / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["files"][mirror.relative_to(image.root).as_posix()] = file_sha256(mirror)
    path.write_text(json.dumps(manifest))
    return replace(image, manifest_digest=file_sha256(path)), mirror


def test_materialized_linux_lib64_keeps_one_application_identity(
    identity_image: tuple[VerifiedRelease, Path],
) -> None:
    image, member = identity_image
    image, mirror = _mirrored_identity(image, member)
    assert not (image.root / "venv/lib64").is_symlink()
    assert mirror.read_bytes() == member.read_bytes()
    assert read_application_identity(image, "a" * 40).source_commit == "a" * 40


@pytest.mark.parametrize(
    "defect", ["changed", "conflicting", "foreign", "non-linux", "missing-primary"]
)
def test_identity_mirror_never_hides_drift_or_another_install(
    identity_image: tuple[VerifiedRelease, Path], defect: str
) -> None:
    image, member = identity_image
    image, mirror = _mirrored_identity(image, member)
    path = image.root / "manifest.json"
    manifest = json.loads(path.read_bytes())
    if defect in {"changed", "conflicting"}:
        mirror.write_bytes(
            mirror.read_bytes().replace(b'"source_commit":"a', b'"source_commit":"b')
        )
        if defect == "conflicting":
            manifest["files"][mirror.relative_to(image.root).as_posix()] = file_sha256(mirror)
    elif defect == "foreign":
        foreign = image.root / "venv/another-install/shared/release-build.json"
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(member.read_bytes())
        manifest["files"][foreign.relative_to(image.root).as_posix()] = file_sha256(foreign)
    elif defect == "non-linux":
        manifest["platform"] = "Darwin-fixture"
    else:
        del manifest["files"][member.relative_to(image.root).as_posix()]
        member.unlink()
    path.write_text(json.dumps(manifest))
    image = replace(image, manifest_digest=file_sha256(path))
    with pytest.raises(ReleaseRejectedError, match=r"identity|inventory"):
        read_application_identity(image, "a" * 40)


@pytest.mark.parametrize("override", ["replace", "attributes", "environment"])
def test_ambient_git_configuration_cannot_change_committed_payload(
    committed_repo: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: str
) -> None:
    repo, commit = committed_repo
    if override == "replace":
        (repo / "shared/__init__.py").write_text('VALUE = "replacement"\n')
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "-qm",
                "replacement",
            ],
            check=True,
        )
        subprocess.run(["git", "-C", str(repo), "replace", commit, "HEAD"], check=True)
    elif override == "attributes":
        (repo / ".git/info/attributes").write_text("shared/__init__.py export-ignore\n")
    else:
        monkeypatch.setenv("GIT_DIR", str(tmp_path / "nonexistent-git"))
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.attributesFile")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(tmp_path / "foreign-attributes"))
    output = tmp_path.resolve() / "output"
    result = build.build_application(
        repo,
        commit,
        output,
        uv=_builder(tmp_path),
        python=Path(sys.executable),
        build_constraints=_constraints(tmp_path),
    )
    with zipfile.ZipFile(result.wheel) as wheel:
        assert wheel.read("shared/__init__.py") == b'VALUE = "committed"\n'


def test_real_sized_manifest_is_read_with_its_own_budget(
    identity_image: tuple[VerifiedRelease, Path],
) -> None:
    image, _ = identity_image
    manifest = image.root / "manifest.json"
    raw = json.loads(manifest.read_bytes())
    raw["files"].update({f"venv/test-{i}.py": "f" * 64 for i in range(70000)})
    manifest.write_text(json.dumps(raw))
    assert manifest.stat().st_size > 5 * 1024 * 1024
    image = VerifiedRelease(
        image.digest, file_sha256(manifest), image.root, image.interpreter, image.cwd
    )
    assert read_application_identity(image, "a" * 40).source_commit == "a" * 40


@pytest.mark.parametrize("defect", ["missing", "changed", "duplicate", "extra"])
def test_wheel_must_preserve_exact_migration_inventory(tmp_path: Path, defect: str) -> None:
    source = tmp_path / "source"
    migrations = source / "migrations"
    migrations.mkdir(parents=True)
    name = "20260924T000000_test.sql"
    (migrations / name).write_text("SELECT 1;\n")
    wheel = tmp_path / "test.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        if defect != "missing":
            archive.writestr(
                "migrations/" + name, "SELECT 2;\n" if defect == "changed" else "SELECT 1;\n"
            )
        if defect == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr("migrations/" + name, "SELECT 1;\n")
        if defect == "extra":
            archive.writestr("migrations/20260924T000001_extra.sql", "SELECT 1;\n")
    with zipfile.ZipFile(wheel) as archive, pytest.raises(ReleaseRejectedError, match="migration"):
        build._verify_migrations(archive, {"migrations/" + name: b"SELECT 1;\n"})


@pytest.mark.parametrize("defect", ["committed-export-ignore", "backend-rewrite"])
def test_migration_expectations_precede_archive_filters_and_build_hooks(
    committed_repo: tuple[Path, str], tmp_path: Path, defect: str
) -> None:
    repo, _ = committed_repo
    (repo / "migrations/20260924T000000_test.sql").write_text("CREATE TABLE example(id int);\n")
    (repo / "migrations/20260924T000000_test.down.sql").write_text("DROP TABLE example;\n")
    if defect == "committed-export-ignore":
        (repo / ".gitattributes").write_text("migrations/** export-ignore\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "schema",
        ],
        check=True,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    with pytest.raises(ReleaseRejectedError, match="migration"):
        build.build_application(
            repo,
            commit,
            tmp_path.resolve() / "output",
            uv=_builder(tmp_path, rewrite_sql=defect == "backend-rewrite"),
            python=Path(sys.executable),
            build_constraints=_constraints(tmp_path),
        )
    assert not (tmp_path / "output/build-receipt.json").exists()


def test_depth_one_checkout_builds_without_parent_objects(
    committed_repo: tuple[Path, str], tmp_path: Path
) -> None:
    repo, _ = committed_repo
    (repo / "shared/__init__.py").write_text('VALUE = "second commit"\n')
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "second",
        ],
        check=True,
    )
    clone = tmp_path.resolve() / "shallow"
    subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "clone",
            "-q",
            "--depth=1",
            repo.as_uri(),
            str(clone),
        ],
        check=True,
    )
    assert (clone / ".git/shallow").is_file()
    commit = subprocess.check_output(
        ["git", "-C", str(clone), "rev-parse", "HEAD"], text=True
    ).strip()
    result = build.build_application(
        clone,
        commit,
        tmp_path.resolve() / "output",
        uv=_builder(tmp_path),
        python=Path(sys.executable),
        build_constraints=_constraints(tmp_path),
    )
    with zipfile.ZipFile(result.wheel) as wheel:
        assert wheel.read("shared/__init__.py") == b'VALUE = "second commit"\n'

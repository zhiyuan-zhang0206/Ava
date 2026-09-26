"""Use uv and pip for locked acquisition; retain every source-to-wheel edge."""

from __future__ import annotations

import email
import json
import re
import shutil
import tomllib
import zipfile
from pathlib import Path

from packaging.utils import canonicalize_name, parse_sdist_filename, parse_wheel_filename

from cli.release_prepare.acquisition_models import WheelDerivation
from cli.release_prepare.acquisition_process import Commands
from cli.release_prepare.inputs import require_directory
from cli.release_prepare.models import FileInput, TreeInput
from cli.release_prepare.source_distributions import validate_distributions
from shared.runtime_prepare import _python_input_inventory, inventory_digest, tree_inventory
from shared.runtime_release import ReleaseRejectedError, file_sha256


def file_input(path: Path) -> FileInput:
    return FileInput(path=path, digest=file_sha256(path))


def tree_input(path: Path) -> TreeInput:
    require_directory(path)
    return TreeInput(root=path, digest=inventory_digest(tree_inventory(path)))


def managed_python(commands: Commands, source: Path) -> tuple[TreeInput, Path, str]:
    version = (source / ".python-version").read_text().strip()
    if re.fullmatch(r"3\.12\.[0-9]+", version) is None:
        raise ReleaseRejectedError("captured source requires an approved exact Python 3.12 pin")
    commands.package("python", "install", "--no-bin", "--no-registry", version, cwd=source)
    executable = Path(
        commands.package("python", "find", "--managed-python", version, cwd=source)
    ).resolve(strict=True)
    if not executable.is_relative_to(commands.work / "python"):
        raise ReleaseRejectedError("managed Python escaped the acquisition directory")
    facts = json.loads(
        commands.run(
            [
                str(executable),
                "-I",
                "-B",
                "-c",
                "import json,sys;print(json.dumps([sys.prefix,'.'.join(map(str,sys.version_info[:3]))]))",
            ],
            source,
        )
    )
    root = Path(facts[0]).resolve(strict=True)
    if facts[1] != version or not root.is_relative_to(commands.work / "python"):
        raise ReleaseRejectedError("acquired Python differs from the captured source pin")
    bundle = TreeInput(root=root, digest=inventory_digest(_python_input_inventory(root)))
    return bundle, executable, version


def build_environment(
    commands: Commands, python: Path, constraints: Path
) -> tuple[Path, str, TreeInput]:
    root = commands.work / "build-environment"
    commands.run([str(python), "-I", "-B", "-m", "venv", str(root)], commands.work)
    executable = root / "bin/python"
    # ensurepip is bundled with this exact managed interpreter; no floating pip install.
    pip_version = commands.run([str(executable), "-I", "-B", "-m", "pip", "--version"], root)
    wheels = commands.work / "build-tools"
    wheels.mkdir(mode=0o700)
    commands.run(
        [
            str(executable),
            "-I",
            "-B",
            "-m",
            "pip",
            "download",
            "--only-binary",
            ":all:",
            "--require-hashes",
            "--index-url",
            "https://pypi.org/simple",
            "-r",
            str(constraints),
            "--dest",
            str(wheels),
        ],
        root,
    )
    commands.package(
        "pip",
        "install",
        "--python",
        str(executable),
        "--require-hashes",
        "--only-binary",
        ":all:",
        "--index-url",
        "https://pypi.org/simple",
        "-r",
        str(constraints),
        cwd=root,
    )
    return executable, pip_version, tree_input(wheels)


def _locked_sources(source: Path) -> dict[str, tuple[str, str]]:
    lock = tomllib.loads((source / "uv.lock").read_text())
    result: dict[str, tuple[str, str]] = {}
    for package in lock["package"]:
        artifacts = package.get("wheels", []) + ([package["sdist"]] if "sdist" in package else [])
        for artifact in artifacts:
            digest = artifact["hash"].removeprefix("sha256:")
            identity = (canonicalize_name(package["name"]), package["version"])
            if digest in result and result[digest] != identity:
                raise ReleaseRejectedError("source lock assigns an artifact to multiple packages")
            result[digest] = identity
    return result


def _wheel_identity(path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as archive:
        names = [
            n for n in archive.namelist() if n.endswith(".dist-info/METADATA") and n.count("/") == 1
        ]
        if len(names) != 1:
            raise ReleaseRejectedError("acquired wheel has no unique metadata")
        metadata = email.message_from_bytes(archive.read(names[0]))
    name, version = metadata["Name"], metadata["Version"]
    if not name or not version or canonicalize_name(name) == "ava":
        raise ReleaseRejectedError("acquisition produced an invalid dependency identity")
    return canonicalize_name(name), version


def _convert(
    commands: Commands, source: Path, identity: tuple[str, str], python: Path, constraints: Path
) -> WheelDerivation:
    captured_source = file_input(source)
    target = commands.work / "wheels"
    if source.suffix == ".whl":
        wheel = target / source.name
        if wheel.exists():
            raise ReleaseRejectedError("duplicate acquired wheel filename")
        shutil.copyfile(source, wheel)
        kind = "downloaded-wheel"
    else:
        output = commands.work / "wheel-builds" / source.name
        output.parent.mkdir(exist_ok=True)
        output.mkdir(mode=0o700)
        commands.package(
            "--offline",
            "build",
            str(source),
            "--wheel",
            "--no-sources",
            "--build-constraints",
            str(constraints),
            "--require-hashes",
            "--python",
            str(python),
            "--out-dir",
            str(output),
            cwd=output,
        )
        candidates = list(output.glob("*.whl"))
        if len(candidates) != 1 or (target / candidates[0].name).exists():
            raise ReleaseRejectedError("sdist build did not produce one unique wheel")
        wheel = target / candidates[0].name
        shutil.copyfile(candidates[0], wheel)
        kind = "built-wheel"
    if _wheel_identity(wheel) != identity:
        raise ReleaseRejectedError("wheel identity differs from its locked source artifact")
    if file_input(source) != captured_source:
        raise ReleaseRejectedError("locked source distribution changed during wheel conversion")
    return WheelDerivation(
        kind=kind,
        source=captured_source,
        wheel=file_input(wheel),
        package=identity[0],
        package_version=identity[1],
    )


def dependencies(
    commands: Commands,
    source: Path,
    python: Path,
    constraints: Path,
    *,
    seed: TreeInput | None = None,
) -> tuple[FileInput, TreeInput, FileInput, tuple[WheelDerivation, ...]]:
    exported = commands.work / "source-requirements.txt"
    before = file_sha256(source / "uv.lock")
    commands.package(
        "export",
        "--locked",
        "--no-dev",
        "--no-emit-project",
        "--no-header",
        "--no-annotate",
        "--format",
        "requirements-txt",
        "--output-file",
        str(exported),
        cwd=source,
    )
    if file_sha256(source / "uv.lock") != before:
        raise ReleaseRejectedError("locked export changed the captured source lock")
    exported_input = file_input(exported)
    downloads = commands.work / "source-distributions"
    downloads.mkdir(mode=0o700)
    source_args = _distribution_source(seed, exported)
    commands.run(
        [
            str(python),
            "-I",
            "-B",
            "-m",
            "pip",
            "download",
            "--no-deps",
            "--no-build-isolation",
            "--require-hashes",
            *source_args,
            "-r",
            str(exported),
            "--dest",
            str(downloads),
        ],
        source,
        timeout=1800,
    )
    expected = _locked_sources(source)
    (commands.work / "wheels").mkdir(mode=0o700)
    edges: list[WheelDerivation] = []
    identities: set[tuple[str, str]] = set()
    for artifact in sorted(downloads.iterdir()):
        digest = file_sha256(artifact)
        if digest not in expected or expected[digest] in identities:
            raise ReleaseRejectedError(
                "downloaded artifact is absent or duplicated in the source lock"
            )
        parsed = (
            parse_wheel_filename(artifact.name)[:2]
            if artifact.suffix == ".whl"
            else parse_sdist_filename(artifact.name)
        )
        if (str(parsed[0]), str(parsed[1])) != expected[digest]:
            raise ReleaseRejectedError("downloaded filename differs from its locked package")
        identities.add(expected[digest])
        edges.append(_convert(commands, artifact, expected[digest], python, constraints))
    if not edges:
        raise ReleaseRejectedError("locked acquisition produced no dependency wheels")
    requirements = commands.work / "requirements.txt"
    requirements.write_text(
        "".join(
            f"{edge.package}=={edge.package_version} --hash=sha256:{edge.wheel.digest}\n"
            for edge in sorted(edges, key=lambda item: item.package)
        )
    )
    if file_input(exported) != exported_input:
        raise ReleaseRejectedError("exported requirements changed during acquisition")
    return exported_input, tree_input(downloads), file_input(requirements), tuple(edges)


def _distribution_source(seed: TreeInput | None, exported: Path) -> list[str]:
    if seed is None:
        return ["--index-url", "https://pypi.org/simple"]
    validate_distributions(seed)
    # --no-index disables registry lookup, but pip would still follow direct
    # URLs or nested requirements. Admit only pinned package requirements.
    from packaging.requirements import InvalidRequirement, Requirement

    requirements = exported.read_text().replace("\\\n", " ")
    for line in requirements.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = re.split(r"\s+--hash=", line, maxsplit=1)
        if (
            len(parts) != 2
            or re.fullmatch(r"sha256:[0-9a-f]{64}(?:\s+--hash=sha256:[0-9a-f]{64})*\s*", parts[1])
            is None
        ):
            raise ReleaseRejectedError("seeded requirements require only SHA256 hash options")
        try:
            parsed = Requirement(parts[0].strip())
        except InvalidRequirement as exc:
            raise ReleaseRejectedError("seeded requirements contain nonlocal directives") from exc
        pins = list(parsed.specifier)
        if (
            parsed.url is not None
            or len(pins) != 1
            or pins[0].operator != "=="
            or "*" in pins[0].version
        ):
            raise ReleaseRejectedError("seeded requirements require pinned registry packages")
    return ["--no-index", "--find-links", str(seed.root)]


def verify_derivations(
    source: Path, distributions: TreeInput, wheels: TreeInput, edges: tuple[WheelDerivation, ...]
) -> None:
    expected = _locked_sources(source)
    if set(tree_inventory(distributions.root)) != {edge.source.path.name for edge in edges}:
        raise ReleaseRejectedError("source distribution inventory differs from wheel derivations")
    if set(tree_inventory(wheels.root)) != {edge.wheel.path.name for edge in edges}:
        raise ReleaseRejectedError("wheel inventory differs from source derivations")
    for edge in edges:
        identity = (edge.package, edge.package_version)
        if (
            expected.get(edge.source.digest) != identity
            or _wheel_identity(edge.wheel.path) != identity
        ):
            raise ReleaseRejectedError("wheel derivation does not match the captured source lock")

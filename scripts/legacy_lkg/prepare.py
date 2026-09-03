"""CI-only fixed-base legacy wheel construction; never a deployment entry."""

from __future__ import annotations

import argparse
import email
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def run(argv: list[str], cwd: Path, *, timeout: int = 600) -> str:
    return subprocess.run(  # noqa: S603 — explicit CI commands, no shell or production home.
        argv, cwd=cwd, check=True, capture_output=True, text=True, timeout=timeout
    ).stdout


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def main() -> None:  # noqa: PLR0915 — ordered CI build, immutable input and cold-proof lifetime.
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    args = parser.parse_args()
    checkout = Path(__file__).resolve().parents[2]
    root = args.root.resolve()
    if os.environ.get("GITHUB_ACTIONS") != "true" or not root.is_relative_to(
        Path(os.environ["RUNNER_TEMP"]).resolve()
    ):
        raise RuntimeError("legacy reconstruction is restricted to GitHub runner scratch")
    root.mkdir(mode=0o700)
    files = checkout / "scripts/legacy_lkg"
    manifest = json.loads((files / "manifest.json").read_text())
    patch = files / "compatibility.patch"
    if sha(patch) != manifest["patch_sha256"]:
        raise AssertionError("legacy compatibility patch hash differs")
    tools = args.tools.resolve()
    if run(["git", "rev-parse", "HEAD"], tools).strip() != manifest["preparation_tools_sha"]:
        raise AssertionError("preparation helpers are not the reviewed exact revision")
    source = root / "source"
    run(["git", "worktree", "add", "--detach", str(source), manifest["base_sha"]], checkout)
    run(["git", "apply", "--check", "--index", str(patch)], source)
    run(["git", "apply", "--index", str(patch)], source)
    tree = run(["git", "write-tree"], source).strip()
    if tree != manifest["build_tree"]:
        raise AssertionError("reconstructed source differs from reviewed compatibility tree")
    tracked = run(["git", "ls-files", "-z", "db/schema.sql", "migrations"], source).split("\0")
    sql_files = {name: sha(source / name) for name in tracked if name.endswith(".sql")}
    inventory = {key: manifest[key] for key in ("base_sha", "patch_sha256", "build_tree")}
    inventory["sql"] = sql_files
    write_json(source / "shared/legacy_inventory.json", inventory)
    wheels = root / "wheels"
    wheels.mkdir()
    run([str(args.uv), "build", "--wheel", "--out-dir", str(wheels)], source)
    requirements = root / "source-requirements.txt"
    run(
        [
            str(args.uv),
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--output-file",
            str(requirements),
        ],
        source,
    )
    run(
        [
            str(args.uv),
            "run",
            "--no-project",
            "--with",
            "pip",
            "python",
            "-m",
            "pip",
            "wheel",
            "--require-hashes",
            "-r",
            str(requirements),
            "--wheel-dir",
            str(wheels),
        ],
        source,
        timeout=900,
    )
    locked = []
    application = None
    for wheel in sorted(wheels.glob("*.whl")):
        with zipfile.ZipFile(wheel) as archive:
            (metadata,) = [
                n
                for n in archive.namelist()
                if n.endswith(".dist-info/METADATA") and n.count("/") == 1
            ]
            package = email.message_from_bytes(archive.read(metadata))
        if package["Name"].lower() == "ava":
            if application is not None:
                raise AssertionError("multiple application wheels")
            application = wheel
        else:
            locked.append(f"{package['Name']}=={package['Version']} --hash=sha256:{sha(wheel)}")
    if application is None:
        raise AssertionError("legacy application wheel absent")
    offline = root / "offline-requirements.txt"
    offline.write_text("\n".join(locked) + "\n")

    # Reuse reviewed private Python copy/verification, not another packaging engine.
    sys.path.insert(0, str(tools))
    from shared.runtime_prepare import (
        _copy_verified_python,
        _materialize_venv_links,
        _python_input_inventory,
        tree_inventory,
    )

    home = root / "unit"
    image = home / "releases" / sha(application)
    image.mkdir(parents=True)
    original_python = args.python.resolve().parent.parent
    python_inventory = _python_input_inventory(original_python)
    _copy_verified_python(original_python, image / "python", python_inventory)
    retained = image / "python/bin/python3"
    run(
        [str(retained), "-I", "-B", "-m", "venv", "--copies", "--without-pip", str(image / "venv")],
        root,
    )
    python = image / "venv/bin/python"
    for selection in (["--require-hashes", "-r", str(offline)], ["--no-deps", str(application)]):
        run(
            [
                str(args.uv),
                "--no-cache",
                "pip",
                "install",
                "--python",
                str(python),
                "--offline",
                "--no-index",
                "--find-links",
                str(wheels),
                "--link-mode",
                "copy",
                *selection,
            ],
            root,
        )
    _materialize_venv_links(image / "venv")
    (image / "plugins").mkdir()
    proof = root / "cold_boot.py"
    shutil.copy2(files / "cold_boot.py", proof)
    provenance = manifest | {
        "wheel_sha256": sha(application),
        "sql_inventory_sha256": sha(source / "shared/legacy_inventory.json"),
        "source_lock_sha256": sha(source / "uv.lock"),
        "source_requirements_sha256": sha(requirements),
        "installed_wheels": {p.name: sha(p) for p in wheels.glob("*.whl")},
        "python_input_inventory_sha256": hashlib.sha256(
            json.dumps(python_inventory, sort_keys=True).encode()
        ).hexdigest(),
    }
    write_json(root / "provenance.json", provenance)
    before = tree_inventory(image)
    # Pin retained inputs read-only; all runtime writes must remain under home.
    for path in image.rglob("*"):
        path.chmod(0o500 if path.is_dir() or os.access(path, os.X_OK) else 0o400)
    image.chmod(0o500)
    retired = source.with_name("source-hidden")
    source.rename(retired)
    hidden_checkout = checkout.with_name(checkout.name + "-legacy-hidden")
    checkout.rename(hidden_checkout)
    try:
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(root),
            "AVA_HOME": str(home),
            "GITHUB_ACTIONS": "true",
            "RUNNER_TEMP": os.environ["RUNNER_TEMP"],
            "AVA_LEGACY_PROOF_PG": os.environ["AVA_LEGACY_PROOF_PG"],
            "PYTHONPATH": str(retired),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        subprocess.run(  # noqa: S603 — actual private retained runtime, copied proof script.
            [str(python), "-I", "-B", str(proof)], cwd=root, env=env, check=True, timeout=300
        )
        if tree_inventory(image) != before:
            raise AssertionError("normal legacy ops mutated its sealed image")
        write_json(
            root / "image-proof.json",
            {
                "base": manifest["base_sha"],
                "patch": manifest["patch_sha256"],
                "tree": tree,
                "wheel": sha(application),
                "sourceAbsent": True,
                "imageUnchanged": True,
                "installed_inventory_sha256": hashlib.sha256(
                    json.dumps(before, sort_keys=True).encode()
                ).hexdigest(),
                "fullRollbackProved": False,
            },
        )
    finally:
        hidden_checkout.rename(checkout)
        retired.rename(source)


if __name__ == "__main__":
    main()

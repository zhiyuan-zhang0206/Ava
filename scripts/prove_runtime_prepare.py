"""CI-only real cold/offline preparation proof; never boots a cluster."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

# Script imports project code for orchestration only. Prepared application
# subprocesses use -I and cannot see this checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.runtime_prepare import (
    PrepareInputs,
    _copy_python,
    _python_input_inventory,
    inventory_digest,
    prepare_release,
    verify_loaded_images,
)
from shared.runtime_release import (
    ReleaseRejectedError,
    VerifiedRelease,
    file_sha256,
    verify_release,
)


def prove_checkout_absent(
    root: Path, checkout: Path, application_name: str, release: VerifiedRelease
) -> None:
    """Run the existing CLI with no source checkout at its original path."""
    verifier = root / "verify_runtime_wheel.py"
    shutil.copy2(checkout / "scripts/verify_runtime_wheel.py", verifier)
    consumer = root / "prove_runtime_consumer.py"
    shutil.copy2(checkout / "scripts/prove_runtime_consumer.py", consumer)
    alias = root / "runtime-entry-alias"
    alias.symlink_to(release.root / "venv", target_is_directory=True)
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or Path(os.environ["GITHUB_WORKSPACE"]).resolve() != checkout
    ):
        raise RuntimeError(
            "checkout-retirement proof is restricted to the exact GitHub CI checkout"
        )
    retired_checkout = checkout.with_name(checkout.name + "-inactive-prepare-proof")
    if retired_checkout.exists():
        raise RuntimeError("checkout retirement destination already exists")
    child_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(root),
        "AVA_CONFIG_FETCH": "skip",
        "AVA_TIMEZONE": "UTC",
        "AVA_HOME": str(root / "probe-home"),
        "AVA_DB_URL": "postgresql://unused@127.0.0.1:1/unused",
        "AVA_REDIS_URL": "redis://127.0.0.1:1/0",
    }
    checkout.rename(retired_checkout)
    try:
        subprocess.run(  # noqa: S603 — copied proof with the installed generation only.
            [str(alias / "bin/python"), "-I", "-B", str(consumer), str(root), str(alias)],
            cwd=root,
            env=child_env,
            check=True,
            timeout=120,
        )
        subprocess.run(  # noqa: S603 — verified generation interpreter, no shell.
            [
                str(release.interpreter),
                "-I",
                "-B",
                str(verifier),
                str(root / "retired-wheels" / application_name),
                "--checkout",
                str(checkout),
            ],
            cwd=root,
            env=child_env,
            check=True,
            timeout=120,
        )
        launched = subprocess.run(  # noqa: S603 — same verified generation, existing CLI consumer.
            release.module_argv("cli.main", "--help"),
            cwd=root,
            env=child_env,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        if "usage:" not in launched.stdout.lower():
            raise AssertionError("existing CLI did not execute from prepared generation")
    finally:
        retired_checkout.rename(checkout)


def prepare_with_diagnostics(store: Path, inputs: PrepareInputs) -> VerifiedRelease:
    try:
        return prepare_release(store, inputs)
    except ReleaseRejectedError as exc:
        candidates = [str(path.relative_to(store)) for path in store.rglob("*.pth")]
        exc.add_note(f"retained .pth inventory: {candidates}")
        raise


def prove_copy_race(
    store: Path, inputs: PrepareInputs, requirements: Path, serving: Path, original: bytes
) -> None:
    """Reject corruption in the private copy before invoking any copied executable."""
    from dataclasses import replace

    directory_alias = inputs.python_tree / "unsupported-directory-alias"
    directory_alias.symlink_to(inputs.python_tree / "bin", target_is_directory=True)
    try:
        try:
            _python_input_inventory(inputs.python_tree)
        except ReleaseRejectedError as exc:
            if str(exc) != "Python input directory symlinks are unsupported":
                raise AssertionError("directory link failed for the wrong reason") from exc
        else:
            raise AssertionError("directory-link input was silently accepted")
    finally:
        directory_alias.unlink()
    requirements.write_text(requirements.read_text() + "\n# copy-race negative\n")
    race_inputs = replace(inputs, requirements_digest=file_sha256(requirements))

    def corrupt_private_copy(source: Path, target: Path) -> None:
        _copy_python(source, target)
        # Input remains pristine: checking its digest again would miss this.
        with (target / "bin/python3").open("ab") as stream:
            stream.write(b"untrusted-copy-race")

    with (
        patch("shared.runtime_prepare._copy_python", side_effect=corrupt_private_copy),
        patch("shared.runtime_prepare._run") as execute,
    ):
        try:
            prepare_release(store, race_inputs)
        except ReleaseRejectedError as exc:
            if str(exc) != "retained Python bytes differ from trusted input inventory":
                raise AssertionError("copy-race failed for the wrong reason") from exc
        else:
            raise AssertionError("corrupted retained Python was accepted")
        execute.assert_not_called()
    if serving.read_bytes() != original:
        raise AssertionError("copy-race rejection changed serving pointer")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    source = Path(
        subprocess.check_output(  # noqa: S603 — CI supplies a managed interpreter path.
            [str(args.python), "-I", "-c", "import sys;print(sys.base_prefix)"], text=True
        ).strip()
    )
    private_python = root / "python-input"
    shutil.copytree(source, private_python, symlinks=True)
    wheels = root / "wheels"
    (application,) = wheels.glob("ava-*.whl")
    requirements = root / "requirements.txt"
    store = root / "store"
    store.mkdir(mode=0o700)
    # Sentinel is intentionally not a real active image: preparation must never
    # read/replace it, even when it cannot understand the serving generation.
    serving = store / "current-release"
    serving.write_bytes(b"existing-serving-generation\n")
    original = serving.read_bytes()
    files = {
        p.relative_to(private_python).as_posix(): file_sha256(p)
        for p in sorted(private_python.rglob("*"))
        if p.is_file()
    }
    wheel_files = {p.name: file_sha256(p) for p in wheels.iterdir()}
    checkout = Path(__file__).resolve().parents[1]
    inputs = PrepareInputs(
        private_python,
        inventory_digest(files),
        wheels,
        inventory_digest(wheel_files),
        requirements,
        file_sha256(requirements),
        application.name,
        file_sha256(checkout / "db/schema.sql"),
        args.uv.resolve(),
    )
    release = prepare_with_diagnostics(store, inputs)
    if serving.read_bytes() != original:
        raise AssertionError("successful preparation changed serving pointer")
    # Remove the input locations from their original names, proving that a
    # prepared generation does not depend on input wheels or base Python paths.
    private_python.rename(root / "retired-python-input")
    wheels.rename(root / "retired-wheels")
    prove_checkout_absent(root, checkout, application.name, release)
    verify_loaded_images(release)
    import platform

    verify_release(
        store,
        release.digest,
        manifest_digest=release.manifest_digest,
        platform_tag=platform.platform(),
        schema_digest=inputs.schema_digest,
    )
    # A fresh generation fails after allocation but before venv creation. No
    # pointer change, cleanup of serving state, or fallback is permitted.
    (root / "retired-python-input").rename(private_python)
    (root / "retired-wheels").rename(wheels)
    requirements.write_text(requirements.read_text() + "\n# failure-injection input\n")
    from dataclasses import replace

    failed_inputs = replace(inputs, requirements_digest=file_sha256(requirements))
    with patch(
        "shared.runtime_prepare._run",
        side_effect=ReleaseRejectedError("injected preparation failure"),
    ):
        try:
            prepare_release(store, failed_inputs)
        except ReleaseRejectedError as exc:
            if str(exc) != "injected preparation failure":
                raise AssertionError("unexpected preparation failure") from exc
        else:
            raise AssertionError("failed preparation was accepted")
    if serving.read_bytes() != original:
        raise AssertionError("failed preparation changed serving pointer")
    prove_copy_race(store, inputs, requirements, serving, original)
    evidence = {
        "corrupt_private_python_rejected_before_execution": True,
        "cold_offline_prepare": True,
        "input_paths_retired_imports": True,
        "checkout_absent_imports_and_existing_cli": True,
        "serving_pointer_unchanged_success_and_failure": True,
        "artifact_digest": release.digest,
        "manifest_digest": release.manifest_digest,
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "python_abi": subprocess.check_output(  # noqa: S603 — verified interpreter.
            [
                str(release.interpreter),
                "-I",
                "-B",
                "-c",
                "import sysconfig;print(sysconfig.get_config_var('SOABI'))",
            ],
            text=True,
        ).strip(),
        "file_count": len(json.loads((release.root / "manifest.json").read_text())["files"]),
    }
    (root / "proof.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence))


if __name__ == "__main__":
    main()

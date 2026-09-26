"""Filesystem contract tests, runnable in CI without pytest/cluster fixtures."""

import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from shared.runtime_release import (
    ReleaseRejectedError,
    activate_release,
    current_pointer,
    file_sha256,
    verify_release,
)


class ReleaseStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Path(self.temporary.name).resolve()
        self.schema = "e" * 64

    def make_release(self, content: bytes) -> tuple[str, str]:
        artifact = hashlib.sha256(content).hexdigest()
        root = self.store / artifact
        (root / "runtime").mkdir(parents=True)
        (root / "runtime" / "python").write_bytes(content)
        (root / "runtime" / "kernel.py").write_bytes(b"value = 1\n")
        manifest = {
            "version": 1,
            "artifact_digest": artifact,
            "platform": "test-platform",
            "schema_digest": self.schema,
            "interpreter": "runtime/python",
            "cwd": "runtime",
            "files": {
                path.relative_to(root).as_posix(): file_sha256(path)
                for path in (root / "runtime").iterdir()
            },
        }
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return artifact, file_sha256(root / "manifest.json")

    def activate(self, release: tuple[str, str], expected: tuple[str, str] | None = None) -> None:
        activate_release(
            self.store,
            release[0],
            manifest_digest=release[1],
            expected_current=expected,
            platform_tag="test-platform",
            schema_digest=self.schema,
        )

    def test_symlink_ancestor_cannot_redirect_verified_generation(self) -> None:
        release = self.make_release(b"alias")
        alias = self.store / "ancestor-alias"
        try:
            alias.symlink_to(self.store.parent, target_is_directory=True)
        except OSError:
            self.skipTest("host does not permit directory symlinks")
        redirected = alias / self.store.name
        self.assertFalse(redirected.is_symlink())
        with self.assertRaises(ReleaseRejectedError):
            verify_release(
                redirected,
                release[0],
                manifest_digest=release[1],
                platform_tag="test-platform",
                schema_digest=self.schema,
            )
        with self.assertRaises(ReleaseRejectedError):
            activate_release(
                redirected,
                release[0],
                expected_current=None,
                manifest_digest=release[1],
                platform_tag="test-platform",
                schema_digest=self.schema,
            )
        self.assertFalse((self.store / "activation.lock").exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX special-file proof")
    def test_unlisted_fifo_is_not_a_complete_inventory(self) -> None:
        release = self.make_release(b"fifo")
        os.mkfifo(self.store / release[0] / "unexpected-pipe")
        with self.assertRaisesRegex(ReleaseRejectedError, "special file"):
            self.activate(release)

    def test_switch_and_rollback_keep_old_absolute_paths(self) -> None:
        first = self.make_release(b"first")
        second = self.make_release(b"second")
        self.activate(first)
        old = verify_release(
            self.store,
            first[0],
            manifest_digest=first[1],
            platform_tag="test-platform",
            schema_digest=self.schema,
        )
        self.activate(second, first)
        self.assertEqual(current_pointer(self.store), second)
        self.assertEqual(old.interpreter.read_bytes(), b"first")
        self.assertEqual(
            old.module_argv("services.agent_host.daemon", "--role", "agent-runner"),
            (
                str(old.interpreter),
                "-I",
                "-B",
                "-X",
                "utf8",
                "-m",
                "services.agent_host.daemon",
                "--role",
                "agent-runner",
            ),
        )
        with self.assertRaisesRegex(ReleaseRejectedError, "entry point"):
            old.module_argv("-c")
        self.activate(first, second)
        self.assertEqual(current_pointer(self.store), first)

    def test_stale_writer_does_not_replace_pointer(self) -> None:
        first = self.make_release(b"first")
        second = self.make_release(b"second")
        self.activate(first)
        with self.assertRaisesRegex(ReleaseRejectedError, "predecessor"):
            self.activate(second)
        self.assertEqual(current_pointer(self.store), first)

    def test_corrupt_member_rejected_before_activation(self) -> None:
        release = self.make_release(b"first")
        (self.store / release[0] / "runtime" / "kernel.py").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ReleaseRejectedError, "hash mismatch"):
            self.activate(release)
        self.assertIsNone(current_pointer(self.store))

    def test_unlisted_file_rejected(self) -> None:
        release = self.make_release(b"first")
        (self.store / release[0] / "runtime" / "injected.py").touch()
        with self.assertRaisesRegex(ReleaseRejectedError, "inventory"):
            self.activate(release)

    def test_manifest_tampering_rejected(self) -> None:
        release = self.make_release(b"first")
        path = self.store / release[0] / "manifest.json"
        path.write_text(path.read_text() + " ")
        with self.assertRaisesRegex(ReleaseRejectedError, "manifest digest"):
            self.activate(release)

    def test_schema_and_platform_mismatch_rejected(self) -> None:
        release = self.make_release(b"first")
        for platform_tag, schema in (("other", self.schema), ("test-platform", "f" * 64)):
            with (
                self.subTest(platform=platform_tag, schema=schema),
                self.assertRaisesRegex(ReleaseRejectedError, "incompatible"),
            ):
                verify_release(
                    self.store,
                    release[0],
                    manifest_digest=release[1],
                    platform_tag=platform_tag,
                    schema_digest=schema,
                )

    def test_failed_replace_preserves_old_pointer(self) -> None:
        first = self.make_release(b"first")
        second = self.make_release(b"second")
        self.activate(first)
        with (
            patch(
                "shared.runtime_release.Path.replace",
                side_effect=PermissionError("locked Windows pointer"),
            ),
            self.assertRaises(PermissionError),
        ):
            self.activate(second, first)
        self.assertEqual(current_pointer(self.store), first)
        self.assertEqual(list(self.store.glob(".current-release-*")), [])

    def test_path_escape_is_rejected_even_with_matching_manifest_hash(self) -> None:
        release = self.make_release(b"first")
        path = self.store / release[0] / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["interpreter"] = "../outside"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ReleaseRejectedError, "unsafe release path"):
            self.activate((release[0], file_sha256(path)))

    def test_hardlinked_runtime_is_rejected(self) -> None:
        release = self.make_release(b"first")
        root = self.store / release[0]
        os.link(root / "runtime" / "kernel.py", self.store / "external-cache")
        with self.assertRaisesRegex(ReleaseRejectedError, "private regular file"):
            self.activate(release)

    def test_path_injection_rejected_even_when_inventoried(self) -> None:
        release = self.make_release(b"first")
        root = self.store / release[0]
        path = root / "manifest.json"
        manifest = json.loads(path.read_text())
        injection = root / "runtime" / "site-packages" / "editable.pth"
        injection.parent.mkdir()
        injection.write_text("/mutable/source\n")
        manifest["files"]["runtime/site-packages/editable.pth"] = file_sha256(injection)
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ReleaseRejectedError, "path injection"):
            self.activate((release[0], file_sha256(path)))

    def test_inert_pth_fixture_is_not_a_startup_hook(self) -> None:
        release = self.make_release(b"first")
        root = self.store / release[0]
        fixture = root / "runtime" / "test-fixture.pth"
        fixture.write_text("/not-an-active-site-path\n")
        self.activate(self.refresh_manifest(release))

    def refresh_manifest(self, release: tuple[str, str]) -> tuple[str, str]:
        root = self.store / release[0]
        path = root / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["files"] = {
            item.relative_to(root).as_posix(): file_sha256(item)
            for item in root.rglob("*")
            if item.is_file() and item != path
        }
        path.write_text(json.dumps(manifest))
        return release[0], file_sha256(path)

    def test_setuptools_hook_requires_original_wheel_bytes(self) -> None:
        release = self.make_release(b"first")
        root = self.store / release[0]
        site = root / "runtime/site-packages"
        (site / "_distutils_hack").mkdir(parents=True)
        helper = site / "distutils-precedence.pth"
        helper.write_bytes(b"import _distutils_hack\n")
        module = site / "_distutils_hack/__init__.py"
        module.write_bytes(b"# wheel-owned helper\n")
        (root / "wheel-evidence").mkdir()
        with zipfile.ZipFile(root / "wheel-evidence/setuptools.whl", "w") as archive:
            archive.writestr(helper.name, helper.read_bytes())
            archive.writestr("_distutils_hack/__init__.py", module.read_bytes())
        original = self.refresh_manifest(release)
        self.activate(original)
        helper.write_bytes(b"/mutable/source\n")
        # Even a new self-reported installed inventory cannot authorize bytes
        # that differ from the retained original wheel.
        with self.assertRaisesRegex(ReleaseRejectedError, "path injection"):
            self.activate(self.refresh_manifest(release), expected=original)


if __name__ == "__main__":
    unittest.main()

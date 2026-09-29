"""Filesystem contract tests, runnable in CI without pytest/cluster fixtures."""

import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from shared.runtime_abi import AbiTag
from shared.runtime_release import (
    MANIFEST_VERSION,
    ReleaseRejectedError,
    activate_release,
    current_pointer,
    file_sha256,
    release_abi,
    verify_release,
)

# A simulated host: the store contract runs on Linux, macOS and Windows alike.
_PY = ("cpython-312", "")
_HOST = AbiTag("linux", "x86_64", "glibc", "2.39", None, None, *_PY)


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
            "version": MANIFEST_VERSION,
            "artifact_digest": artifact,
            "abi_tag": _HOST.to_json(),
            "platform": "Linux-6.8.0-45-generic-x86_64-with-glibc2.39",
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
            host_abi=_HOST,
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
                host_abi=_HOST,
                schema_digest=self.schema,
            )
        with self.assertRaises(ReleaseRejectedError):
            activate_release(
                redirected,
                release[0],
                expected_current=None,
                manifest_digest=release[1],
                host_abi=_HOST,
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
            host_abi=_HOST,
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

    def test_schema_mismatch_rejected(self) -> None:
        release = self.make_release(b"first")
        with self.assertRaisesRegex(ReleaseRejectedError, "schema differs"):
            self.verify_on(release, _HOST, schema="f" * 64)

    def rewrite_manifest(self, release: tuple[str, str], **changes: object) -> tuple[str, str]:
        path = self.store / release[0] / "manifest.json"
        manifest = json.loads(path.read_text())
        for name, value in changes.items():
            if value is None:
                del manifest[name]
            else:
                manifest[name] = value
        path.write_text(json.dumps(manifest))
        return release[0], file_sha256(path)

    def verify_on(self, release: tuple[str, str], host: AbiTag, schema: str = "") -> None:
        verify_release(
            self.store,
            release[0],
            manifest_digest=release[1],
            host_abi=host,
            schema_digest=schema or self.schema,
        )

    def test_abi_tag_round_trips_and_platform_string_is_provenance_only(self) -> None:
        release = self.make_release(b"first")
        image = verify_release(
            self.store,
            release[0],
            manifest_digest=release[1],
            host_abi=_HOST,
            schema_digest=self.schema,
        )
        self.assertEqual(release_abi(image), _HOST)
        # A kernel or OS patch changes the provenance string, and a glibc
        # upgrade raises the host above the floor: the image stays bootable.
        patched = self.rewrite_manifest(release, platform="Linux-6.11.0-99-generic-x86_64")
        self.verify_on(patched, AbiTag("linux", "x86_64", "glibc", "2.41", None, None, *_PY))
        self.activate(patched)
        self.assertEqual(current_pointer(self.store), patched)

    def test_incompatible_host_abi_rejected_before_activation(self) -> None:
        release = self.make_release(b"first")
        hosts = {
            "arch": AbiTag("linux", "aarch64", "glibc", "2.39", None, None, *_PY),
            "older glibc": AbiTag("linux", "x86_64", "glibc", "2.35", None, None, *_PY),
            "python": AbiTag("linux", "x86_64", "glibc", "2.39", None, None, "cpython-313", ""),
            "os": AbiTag("macos", "x86_64", None, None, "26", None, *_PY),
        }
        for label, host in hosts.items():
            with (
                self.subTest(label),
                self.assertRaisesRegex(ReleaseRejectedError, "incompatible with this host"),
            ):
                self.verify_on(release, host)
        self.assertIsNone(current_pointer(self.store))

    def test_manifest_without_abi_tag_refuses_clearly(self) -> None:
        release = self.make_release(b"first")
        legacy = self.rewrite_manifest(release, abi_tag=None, version=1)
        with self.assertRaisesRegex(ReleaseRejectedError, "no abi_tag.*re-prepare"):
            self.verify_on(legacy, _HOST)
        stale = self.rewrite_manifest(legacy, abi_tag=_HOST.to_json())
        with self.assertRaisesRegex(ReleaseRejectedError, "shape/version"):
            self.verify_on(stale, _HOST)

    def test_malformed_abi_tag_refuses(self) -> None:
        release = self.make_release(b"first")
        for label, tag in {
            "unknown field": {**_HOST.to_json(), "kernel": "6.8"},
            "unknown libc": {**_HOST.to_json(), "libc": "musl"},
            "numeric floor": {**_HOST.to_json(), "libc_version": 2.39},
        }.items():
            with (
                self.subTest(label),
                self.assertRaisesRegex(ReleaseRejectedError, "invalid release abi_tag"),
            ):
                self.verify_on(self.rewrite_manifest(release, abi_tag=tag), _HOST)

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

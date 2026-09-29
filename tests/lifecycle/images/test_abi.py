"""Release ABI tag computation and compatibility, without a cluster or pytest.

Linux and macOS facts are simulated on every host; only the real-host round trip
depends on where the suite runs.
"""

import sys
import unittest

from base.runtime_abi import (
    AbiTag,
    AbiTagError,
    abi_from_facts,
    abi_refusal,
    current_abi,
    parse_abi_tag,
)

_PY = ("cpython-312", "")


def _linux(
    glibc: str | None = "glibc 2.39",
    machine: str = "x86_64",
    cache_tag: str = "cpython-312",
    abiflags: str = "",
) -> AbiTag:
    return abi_from_facts(
        system="linux",
        machine=machine,
        glibc=glibc,
        mac_release="",
        deployment_target=None,
        cache_tag=cache_tag,
        abiflags=abiflags,
    )


def _macos(release: str = "26.6.2", target: object = "11.0", machine: str = "arm64") -> AbiTag:
    return abi_from_facts(
        system="darwin",
        machine=machine,
        glibc=None,
        mac_release=release,
        deployment_target=target,
        cache_tag="cpython-312",
        abiflags="",
    )


class AbiTagComputationTests(unittest.TestCase):
    def test_linux_facts_name_glibc_floor_not_kernel(self) -> None:
        tag = _linux()
        self.assertEqual(tag, AbiTag("linux", "x86_64", "glibc", "2.39", None, None, *_PY))
        self.assertEqual(str(tag), "linux-x86_64-glibc2.39-cpython-312")
        self.assertEqual(_linux(machine="AARCH64").arch, "aarch64")

    def test_macos_facts_name_major_and_deployment_target(self) -> None:
        tag = _macos()
        self.assertEqual(tag, AbiTag("macos", "arm64", None, None, "26", "11.0", *_PY))
        self.assertEqual(str(tag), "macos-arm64-26-cpython-312")
        self.assertEqual(_macos("15.7.1").macos, "15")
        for unexposed in (None, ""):
            self.assertIsNone(_macos(target=unexposed).macos_deployment_target)

    def test_unknown_or_unsupported_facts_refuse(self) -> None:
        cases = {
            "musl (no glibc confstr)": lambda: _linux(glibc=None),
            "glibc development build": lambda: _linux(glibc="glibc 2.39.9000"),
            "empty architecture": lambda: _linux(machine=""),
            "other interpreter": lambda: _linux(cache_tag="pypy39"),
            "free-threading flag form": lambda: _linux(abiflags="T"),
            "macOS 10 compatibility shim": lambda: _macos("10.16", target=None),
            "unreadable macOS release": lambda: _macos(""),
            "deployment target above major": lambda: _macos("26.1", target="27.0"),
            "non-string deployment target": lambda: _macos(target=11),
            "Windows": lambda: abi_from_facts(
                system="win32",
                machine="AMD64",
                glibc=None,
                mac_release="",
                deployment_target=None,
                cache_tag="cpython-312",
                abiflags="",
            ),
        }
        for label, compute in cases.items():
            with self.subTest(label), self.assertRaises(AbiTagError):
                compute()
        with self.assertRaises(AbiTagError):
            abi_from_facts(
                system="linux",
                machine="x86_64",
                glibc="glibc 2.39",
                mac_release="",
                deployment_target=None,
                cache_tag=None,
                abiflags="",
            )

    def test_current_host_tag_round_trips(self) -> None:
        if sys.platform not in {"linux", "darwin"}:
            with self.assertRaises(AbiTagError):
                current_abi()
            return
        tag = current_abi()
        self.assertEqual(parse_abi_tag(tag.to_json()), tag)
        self.assertEqual(tag.python, sys.implementation.cache_tag)
        self.assertEqual(tag.os, {"linux": "linux", "darwin": "macos"}[sys.platform])
        self.assertIsNone(abi_refusal(tag, current_abi()))

    def test_manifest_form_is_exact(self) -> None:
        good = _linux().to_json()
        cases: dict[str, object] = {
            "not an object": ["linux"],
            "missing field": {k: v for k, v in good.items() if k != "abiflags"},
            "unknown field": {**good, "kernel": "6.8.0"},
            "numeric value": {**good, "libc_version": 2.39},
            "macOS field on Linux": {**good, "macos": "26"},
            "unknown operating system": {**good, "os": "freebsd"},
            "unknown libc": {**good, "libc": "musl"},
        }
        for label, value in cases.items():
            with self.subTest(label), self.assertRaises(AbiTagError):
                parse_abi_tag(value)
        self.assertEqual(parse_abi_tag(good), _linux())


class AbiCompatibilityTests(unittest.TestCase):
    def assert_runs(self, image: AbiTag, host: AbiTag) -> None:
        self.assertIsNone(abi_refusal(image, host))

    def assert_refused(self, image: AbiTag, host: AbiTag, reason: str) -> None:
        refusal = abi_refusal(image, host)
        self.assertIsNotNone(refusal)
        self.assertIn(reason, str(refusal))

    def test_os_patch_kernel_and_upgrades_keep_linux_images(self) -> None:
        image = _linux()
        # The kernel release and OS patch level are not tag inputs at all.
        self.assert_runs(image, _linux())
        self.assert_runs(image, _linux("glibc 2.41"))
        self.assert_runs(_linux("glibc 2.9"), _linux("glibc 2.10"))

    def test_linux_host_below_image_glibc_floor_refuses(self) -> None:
        self.assert_refused(_linux("glibc 2.39"), _linux("glibc 2.35"), "older than the image")
        self.assert_refused(_linux("glibc 2.10"), _linux("glibc 2.9"), "older than the image")

    def test_macos_patch_and_major_upgrade_keep_images(self) -> None:
        image = _macos("26.6.2")
        self.assert_runs(image, _macos("26.7"))
        self.assert_runs(image, _macos("27.0.1"))
        # The host tag's deployment target describes only the verifier.
        self.assert_runs(image, _macos("26.6.2", target="14.0"))

    def test_macos_host_below_image_major_refuses(self) -> None:
        self.assert_refused(_macos("26.0"), _macos("25.4"), "older than the image")

    def test_architecture_os_and_python_abi_must_match(self) -> None:
        self.assert_refused(_linux(), _linux(machine="aarch64"), "arch")
        self.assert_refused(_macos(), _macos(machine="x86_64"), "arch")
        self.assert_refused(_linux(), _linux(cache_tag="cpython-313"), "python")
        self.assert_refused(_linux(), _linux(abiflags="t"), "abiflags")
        self.assert_refused(_linux(), _macos(), "os")


if __name__ == "__main__":
    unittest.main()

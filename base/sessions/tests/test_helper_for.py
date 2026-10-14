"""Which permissions-helper factory a platform runs with: macOS always one, Linux none."""

import pytest

from base.native_process.os_platform import Platform
from base.sessions.helperproc import helper_client, helper_for


def _platform(sys_platform: str) -> Platform:
    return Platform.from_sys_platform(sys_platform, machine="arm64", executable="/usr/bin/python3")


def test_macos_always_runs_with_the_real_helper_factory() -> None:
    assert helper_for(_platform("darwin")) is helper_client


def test_linux_has_no_helper() -> None:
    assert helper_for(_platform("linux")) is None


def test_an_unknown_platform_is_refused_not_given_a_default() -> None:
    with pytest.raises(ValueError, match="unsupported platform"):
        _platform("freebsd")
    # A platform that slipped past its own validation still reaches no silent answer.
    platform = _platform("linux")
    object.__setattr__(platform, "system", "freebsd")
    with pytest.raises(ValueError, match=r"no permissions-helper decision.*freebsd"):
        helper_for(platform)

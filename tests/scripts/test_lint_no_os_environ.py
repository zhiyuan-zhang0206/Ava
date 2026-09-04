"""The raw-environment allowlist must describe live boundary crossings."""

from scripts import lint_no_os_environ


def test_raw_environment_exemptions_are_all_used() -> None:
    assert lint_no_os_environ._unused_file_exemptions() == []

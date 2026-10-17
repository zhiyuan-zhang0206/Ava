from __future__ import annotations

import pytest

from scripts.ci.helper_smoke import signing_smoke as smoke


@pytest.mark.parametrize(
    ("returncode", "text", "expected", "failure"),
    [
        (0, 'Executable=/x\n# designated => cdhash H"ab"', "cdhash", None),
        (1, "code object is not signed at all", "cdhash", "designated requirement readback failed"),
        (0, "identifier com.meiva.helper", "cdhash", "readback has no 'cdhash'"),
        (
            0,
            "<key>com.apple.security.automation.apple-events</key>",
            smoke.APPLE_EVENTS_ENTITLEMENT,
            None,
        ),
        (
            0,
            "<dict/>",
            smoke.APPLE_EVENTS_ENTITLEMENT,
            "no 'com.apple.security.automation.apple-events'",
        ),
    ],
)
def test_readback_failure_names_what_is_missing(
    returncode: int, text: str, expected: str, failure: str | None
) -> None:
    problem = smoke.readback_failure("designated requirement", returncode, text, expected)
    if failure is None:
        assert problem is None
    else:
        assert problem is not None and failure in problem


def test_readback_failure_truncates_long_output() -> None:
    problem = smoke.readback_failure("entitlements", 1, "x" * 5000, "anything")
    assert problem is not None and len(problem) < 600


def test_run_requires_the_keychain_password() -> None:
    with pytest.raises(SystemExit) as refused:
        smoke.main(["run"])
    assert refused.value.code == 2


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit) as refused:
        smoke.main([])
    assert refused.value.code == 2

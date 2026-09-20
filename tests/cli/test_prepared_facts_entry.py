"""The prepared-facts entry refuses before any read or effect on unsupported ground."""

from __future__ import annotations

import pytest

from cli import prepared_facts
from shared.runtime_release import ReleaseRejectedError

ARGV = [
    "--operation-holder",
    "gateway:pid1",
    "--operation-acquired-at",
    "2026-09-20T00:00:00+00:00",
    "--operation-target-sha",
    "0" * 40,
    "--artifact-digest",
    "a" * 64,
    "--manifest-digest",
    "b" * 64,
    "--schema-digest",
    "c" * 64,
    "--recovery-artifact-digest",
    "d" * 64,
    "--recovery-manifest-digest",
    "e" * 64,
    "--recovery-schema-digest",
    "f" * 64,
]


def test_non_linux_platform_refuses_before_any_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Q7: macOS/Windows units fail closed at preparation -- the restricted hop
    has no native proof there, and that refusal must not wait inside the hop."""
    monkeypatch.setattr(prepared_facts.sys, "platform", "darwin")
    args = prepared_facts._parser().parse_args(ARGV)
    with pytest.raises(ReleaseRejectedError, match="restricted hop supports"):
        prepared_facts.produce_facts(args)


def test_source_cannot_impersonate_the_candidate_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prepared_facts.sys, "platform", "linux")
    monkeypatch.setattr(prepared_facts, "WHEEL_RUNTIME", False)
    with pytest.raises(ReleaseRejectedError, match="verified candidate runtime"):
        prepared_facts._loaded_unit()

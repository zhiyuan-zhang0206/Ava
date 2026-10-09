"""Sandbox schema owns output defaults and limit validation."""

import pytest
from pydantic import ValidationError

from base.config.domains.sandbox import SandboxSettings


@pytest.mark.parametrize(
    "field,value",
    [
        ("exec_output_crop_after_lines", -1),
        ("exec_output_crop_after_chars", 0),
        ("exec_output_crop_after_bytes", 0),
        ("exec_output_crop_head_lines", 0),
        ("exec_output_crop_tail_lines", 0),
        ("exec_output_crop_archive_max_bytes", 0),
    ],
)
def test_invalid_crop_config_is_rejected(field: str, value: int):
    with pytest.raises(ValidationError):
        SandboxSettings.model_validate({field: value})


def test_config_defaults_and_disabled_trigger():
    config = SandboxSettings.model_validate({})
    assert config.exec_output_crop_after_lines == 300
    assert config.exec_output_crop_after_chars == 64 * 1024
    assert config.exec_output_crop_after_bytes == 64 * 1024
    assert config.exec_output_crop_head_lines == config.exec_output_crop_tail_lines == 25
    assert config.exec_output_crop_archive_max_bytes == 16 * 1024 * 1024
    assert (
        SandboxSettings.model_validate(
            {"exec_output_crop_after_lines": 0}
        ).exec_output_crop_after_lines
        == 0
    )


def test_crop_size_triggers_must_not_sit_below_the_hard_cap():
    with pytest.raises(ValidationError, match="exec_output_crop_after_chars"):
        SandboxSettings.model_validate({"exec_output_crop_after_chars": 20_000})
    with pytest.raises(ValidationError, match="exec_output_crop_after_bytes"):
        SandboxSettings.model_validate({"exec_output_crop_after_bytes": 20_000})
    config = SandboxSettings.model_validate(
        {"exec_output_crop_after_chars": 30_000, "exec_output_crop_after_bytes": 30_000}
    )
    assert config.exec_output_crop_after_chars == 30_000
    assert config.exec_output_crop_after_bytes == 30_000


# ---------------------------------------------------------------------------
# The budget is a validated settings field
# ---------------------------------------------------------------------------


def test_budget_below_the_inline_cap_is_refused_at_startup() -> None:
    """A budget under `exec_output_max_chars` would hand the envelope less than
    it slices, so its "head" would reach into the accumulator's dropped middle.
    Fail the operator's config loudly instead of rendering an incoherent
    envelope."""
    from pydantic import ValidationError

    from base.config.domains.sandbox import SandboxSettings

    with pytest.raises(ValidationError, match="must be >= exec_output_max_chars"):
        SandboxSettings.model_validate(
            {"exec_output_max_chars": 30_000, "exec_output_accumulation_max_chars": 1000}
        )

    ok = SandboxSettings.model_validate(
        {"exec_output_max_chars": 1000, "exec_output_accumulation_max_chars": 1000}
    )
    assert ok.exec_output_accumulation_max_chars == 1000

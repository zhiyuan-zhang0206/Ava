import pytest
from pydantic import ValidationError

from base.config.domains.to_user import ToUserSettings


def test_limits_default_to_the_widened_bounds() -> None:
    config = ToUserSettings.model_validate({})
    assert config.max_document_bytes == 1024 * 1024
    assert config.max_references == 1024
    assert config.max_chain_depth == 16
    assert config.max_resource_bytes == 64 * 1024 * 1024


@pytest.mark.parametrize(
    "field",
    ["max_document_bytes", "max_references", "max_chain_depth", "max_resource_bytes"],
)
def test_every_limit_must_be_positive(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        ToUserSettings.model_validate({field: 0})

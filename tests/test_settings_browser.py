"""Browser settings defaults — on by default (auto-detect display), port 9222, no forced binary.

Asserts the declared field defaults via `model_fields` rather than constructing
`Settings()` (which needs required env fields and is env-dependent)."""

from shared.config import FIELD_INFOS


def test_browser_defaults() -> None:
    fields = FIELD_INFOS
    assert fields["browser_enabled"].default is True
    assert fields["chrome_binary"].default is None
    # CDP port is now a per-cluster setting (cluster port block); default 9222
    # keeps the legacy single-cluster behavior.
    assert fields["browser_cdp_port"].default == 9222

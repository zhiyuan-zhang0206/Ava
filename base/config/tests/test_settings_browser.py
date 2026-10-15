from base.config import FIELD_INFOS


def test_browser_defaults() -> None:
    fields = FIELD_INFOS
    assert fields["browser_enabled"].default is True
    assert fields["chrome_binary"].default is None
    assert fields["browser_cdp_port"].default == 9222

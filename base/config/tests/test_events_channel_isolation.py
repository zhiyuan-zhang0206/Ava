from base.config import settings


def test_events_channel_is_test_isolated() -> None:
    assert settings.data_plane.events_channel.startswith("meiva:events:test:"), (
        f"settings.data_plane.events_channel={settings.data_plane.events_channel!r} — env_bootstrap's settings.data_plane.events_channel "
        "mutation did not take effect or was cleared. Check whether tests/fixtures/env_bootstrap.py still writes the isolated channel."
    )

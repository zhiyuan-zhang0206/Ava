"""Guard that the test session's events channel was isolated by conftest — publish must
not leak to the dev kernel.

`settings.data_plane.events_channel` is read directly by every caller after the Stage 1
lazy-snapshot fix; the conftest mutation
`settings.data_plane.events_channel = ava:events:test:<...>` takes effect immediately for
all callers.

This assert is retained because **test publishes leaking to the dev Ava Kernel / Terminal**
is a real incident that happened — it pins the invariant "the test session truly got a
channel with a test prefix", so any conftest that accidentally drops the mutation goes
red immediately.
"""

from shared.config import settings


def test_events_channel_is_test_isolated() -> None:
    # conftest sets channel to `ava:events:test:<PID>_<TS>` with distinct suffixes,
    # so concurrent sessions don't cross-talk.
    assert settings.data_plane.events_channel.startswith("ava:events:test:"), (
        f"settings.data_plane.events_channel={settings.data_plane.events_channel!r} — conftest's settings.data_plane.events_channel "
        "mutation did not take effect or was cleared. Check whether the top-level conftest still writes the isolated channel."
    )

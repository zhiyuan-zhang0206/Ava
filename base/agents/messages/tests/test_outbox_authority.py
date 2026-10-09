"""Each sender captures at first send; flushing continues to read its own authority."""

from pathlib import Path

from base.agents.messages.delivery_outbox import DeliverySenderConfig, limits
from base.config import Settings
from base.config.service_read import ConfigAuthority


def _authority(path: Path) -> ConfigAuthority:
    runtime = Settings(
        profile=None,
        data_plane={
            "db_url": "postgresql://ava@127.0.0.1:5433/ava",
            "redis_url": "redis://127.0.0.1:6379/0",
        },
    )
    return ConfigAuthority(runtime=runtime, all_domains=runtime, env_path=path)


def test_sender_captures_on_first_send_and_flush_limits_remain_live(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("AVA_DELIVERY_OUTBOX_ENABLED=false\n")
    authority = _authority(path)
    sender = DeliverySenderConfig(authority)
    path.write_text("AVA_DELIVERY_OUTBOX_ENABLED=true\n")
    assert sender.settings()[0] is True

    path.write_text("AVA_DELIVERY_OUTBOX_ENABLED=false\n")
    assert sender.settings()[0] is True
    assert limits(authority).enabled is False


def test_two_sender_owners_never_share_their_first_send_pair(tmp_path: Path) -> None:
    one_path, two_path = tmp_path / "one.env", tmp_path / "two.env"
    one_path.write_text("AVA_DELIVERY_OUTBOX_ENABLED=true\n")
    two_path.write_text("AVA_DELIVERY_OUTBOX_ENABLED=false\n")
    one, two = (
        DeliverySenderConfig(_authority(one_path)),
        DeliverySenderConfig(_authority(two_path)),
    )
    assert one.settings()[0] is True
    assert two.settings()[0] is False
    one_path.write_text("AVA_DELIVERY_OUTBOX_ENABLED=false\n")
    two_path.write_text("AVA_DELIVERY_OUTBOX_ENABLED=true\n")
    assert one.settings()[0] is True
    assert two.settings()[0] is False
    assert limits(one.authority).enabled is False
    assert limits(two.authority).enabled is True

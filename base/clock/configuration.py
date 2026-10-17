# meiva:note
# A boot ClockConfig captures only its supplied owner's timezone authority and timestamp policy, independently of older Clock handles and other owners.
# meiva:link base/clock/docs/clock.meiva.okf.md
from __future__ import annotations

from base.clock import ClockConfig
from base.config import ConfigBoot


def clock_config_from_boot(boot: ConfigBoot) -> ClockConfig:
    return ClockConfig(
        timezone=boot.view.general.timezone,
        authoritative_timezone=(
            boot.view.general.timezone if boot.field_explicitly_set("timezone") else None
        ),
        message_timestamp_weekday=boot.view.general.message_timestamp_weekday,
    )
